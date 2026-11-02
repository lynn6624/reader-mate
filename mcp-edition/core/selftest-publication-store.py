#!/usr/bin/env python3
"""Synthetic, isolated publication transactions; never touches production data."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

import publication_store as module
from publication_store import Store, PublicationError, ConflictError, ValidationError


NAME = "Publication-fixture.txt"
KEY = hashlib.sha256(NAME.encode()).hexdigest()
IDENT = "pub_fault_fixture_0001"


def payloads(version):
    return {"书库/" + NAME: ("Before 😀\n" if version == 0 else "After 😀\n").encode(),
            "data/notes.json": json.dumps({"version": version, "foreign_author": "revoked_fixture"}).encode(),
            "data/progress.json": json.dumps({"version": version, "other_book": 88}).encode(),
            "data/library.json": json.dumps({"version": version, "category": "literature"}).encode(),
            "data/navigation/" + KEY + ".json": json.dumps({"version": version}).encode(),
            "data/drafts/" + KEY + ".json": json.dumps({"version": version}).encode()}


def fixture(core):
    for relative, raw in payloads(0).items():
        path = core / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    (core / "data/identities.json").write_bytes(b'{"synthetic":"unchanged"}')


def expect(error, action):
    try:
        action()
    except error:
        return
    raise AssertionError("Expected " + error.__name__)


def assert_version(core, version):
    for relative, raw in payloads(version).items():
        assert (core / relative).read_bytes() == raw, (version, relative)
    assert (core / "data/identities.json").read_bytes() == b'{"synthetic":"unchanged"}'


def child(args):
    core = Path(args.core)
    if args.lock_child:
        with Store(core).barrier():
            Path(args.ready).write_bytes(b"ready")
            deadline = time.monotonic() + 15
            while not Path(args.release).exists() and time.monotonic() < deadline:
                time.sleep(.02)
            assert Path(args.release).exists(), "Timed out waiting for synthetic lock release"
        return
    def hook(label, index):
        if label == args.label and index == args.index:
            os._exit(73)
    Store(core, hook=hook).commit(NAME, IDENT, payloads(1), {"action": "publish", "synthetic": True})


def test_crashes(root):
    cases = [(label, -1, committed) for label, committed in (
        ("prepared", False), ("active", False), ("before_commit", False),
        ("after_commit", True), ("before_applied", True), ("after_applied", True),
        ("after_receipt", True), ("after_clear_active", True))]
    cases += [("after_replace", index, True) for index in range(7)]
    for number, (label, index, committed) in enumerate(cases):
        core = root / ("crash-" + str(number))
        fixture(core)
        result = subprocess.run([sys.executable, "-B", __file__, "--child", "--core", str(core),
                                 "--label", label, "--index", str(index)], timeout=30,
                                capture_output=True, text=True)
        assert result.returncode == 73, (label, index, result.stderr)
        store = Store(core)
        store.recover()
        store.recover()
        with store.barrier():
            assert_version(core, 1 if committed else 0)
        if committed:
            result = store.commit(NAME, IDENT, payloads(1), {"action": "publish", "synthetic": True})
            assert result["already_applied"] is True
        else:
            store.commit(NAME, IDENT, payloads(1), {"action": "publish", "synthetic": True})
            assert_version(core, 1)
    print("PASS: 15 process-termination boundaries, repeated recovery and idempotent retry")


def test_recovery_refusal(root):
    for kind in ("target", "payload", "manifest"):
        core = root / ("corrupt-" + kind)
        fixture(core)
        def fail(label, index):
            if label == "after_commit":
                raise OSError("synthetic committed interruption")
        store = Store(core, fail)
        expect(OSError, lambda: store.commit(NAME, IDENT, payloads(1), {"action": "publish"}))
        history = store.root / KEY / IDENT
        if kind == "target":
            (core / "data/notes.json").write_bytes(b"later unrelated state")
        elif kind == "payload":
            (history / "new-0.bin").write_bytes(b"corrupt payload")
        else:
            (history / "manifest.json").write_bytes(b"{}")
        expect(PublicationError, lambda: Store(core).recover())
        for relative, original in payloads(0).items():
            if kind != "target" or relative != "data/notes.json":
                assert (core / relative).read_bytes() == original, (kind, relative)
        if kind == "target":
            assert (core / "data/notes.json").read_bytes() == b"later unrelated state"
        assert (store.root / "active.json").exists()
    print("PASS: conflicting live target / corrupt payload / corrupt manifest block recovery without writes")


def test_pending_reader(root):
    core = root / "pending-reader"
    fixture(core)
    def fail(label, index):
        if label == "after_replace" and index == 2:
            raise OSError("synthetic partial replacement")
    expect(OSError, lambda: Store(core, fail).commit(NAME, IDENT, payloads(1), {"action": "publish"}))
    with Store(core).barrier():
        assert_version(core, 1)
    print("PASS: SH request drops its lock, recovers under EX, then observes a complete generation")


def test_aborted_binding(root):
    core = root / "aborted-binding"
    fixture(core)
    def fail(label, index):
        if label == "before_commit":
            raise OSError("synthetic preparation failure")
    expect(OSError, lambda: Store(core, fail).commit(NAME, IDENT, payloads(1), {}))
    store = Store(core)
    store.recover()
    assert_version(core, 0)
    expect(ConflictError, lambda: store.commit(NAME, IDENT, payloads(1), {"changed": True}))
    store.commit(NAME, IDENT, payloads(1), {})
    assert_version(core, 1)
    print("PASS: discarded preparation retains request binding; changed retry is refused")


def test_contracts_and_cleanup(root):
    core = root / "contracts"
    fixture(core)
    store = Store(core)
    with store.barrier():
        with Store(core).barrier():
            expect(ConflictError, lambda: store.commit(NAME, IDENT, payloads(1), {"action": "publish"}))
    for invalid in ("../notes.json", "data/identities.json", "data/notes.json/../progress.json",
                    "书库/another.txt", "data/publications/" + KEY + "/current.json"):
        changed = dict(payloads(1), **{invalid: b"bad"})
        expect(ValidationError, lambda changed=changed: store.commit(NAME, IDENT, changed, {"action": "publish"}))
    expect(ValidationError, lambda: store.commit("../escape.txt", IDENT, payloads(1), {}))
    ids = ["pub_contract_000" + str(i) for i in range(4)]
    assert store.request_receipt(NAME, "pub_unknown_fixture") is None
    for i in range(3):
        store.commit(NAME, ids[i], payloads(1), {"action": "publish", "sequence": i})
    assert store.current(NAME)["generation"] == 3
    assert len(store.list_history(NAME)) == 3
    assert store.old_payload(NAME, ids[0], "书库/" + NAME) == payloads(0)["书库/" + NAME]
    assert store.new_payload(NAME, ids[0], "书库/" + NAME) == payloads(1)["书库/" + NAME]
    expect(ConflictError, lambda: store.commit(NAME, ids[0], payloads(1), {"action": "publish", "changed": True}))
    expect(ConflictError, lambda: store.commit(NAME, ids[3], payloads(1), {"action": "publish"}))
    expect(ConflictError, lambda: store.preview_cleanup(NAME, [ids[2]]))
    plan = store.preview_cleanup(NAME, [ids[0]])
    expect(ConflictError, lambda: store.cleanup(NAME, [ids[0]], "wrong digest"))
    store.cleanup(NAME, [ids[0]], plan["digest"])
    assert len(store.list_history(NAME)) == 2
    repeated_cleanup = store.cleanup(NAME, [ids[0]], plan["digest"])
    assert repeated_cleanup["already_cleaned"] is True
    expect(ConflictError, lambda: store.cleanup(NAME, ["pub_unknown_fixture"], "a" * 64))
    again = store.commit(NAME, ids[0], payloads(1), {"action": "publish", "sequence": 0})
    assert again["already_applied"] is True and again["history_pruned"] is True
    saved_receipt = store.request_receipt(NAME, ids[0])
    assert saved_receipt["history_pruned"] is True and saved_receipt["already_applied"] is True
    assert saved_receipt["report"]["action"] == "publish"
    assert saved_receipt["generation"] == 1
    expect(ConflictError, lambda: store.commit(NAME, ids[0], payloads(1), {"action": "rollback"}))
    stale = store.preview_cleanup(NAME, [ids[1]])
    store.commit(NAME, ids[3], payloads(1), {"action": "rollback"})
    expect(ConflictError, lambda: store.cleanup(NAME, [ids[1]], stale["digest"]))
    assert store.current(NAME)["generation"] == 4
    assert store.current(NAME)["previous_publication_id"] == ids[2]
    expect(ValidationError, lambda: store.request_receipt(NAME, None))
    expect(ValidationError, lambda: store.request_receipt(NAME, "../bad_request_id"))
    assert_version(core, 1)
    print("PASS: path whitelist, forbidden SH upgrade, history/payload reads, bounded retention and confirmed cleanup")


def test_limits_and_links(root):
    core = root / "limits"
    fixture(core)
    store = Store(core)
    old_limit = module.MAX_HISTORY_BYTES
    module.MAX_HISTORY_BYTES = 100
    try:
        expect(ConflictError, lambda: store.commit(NAME, IDENT, payloads(1), {}))
        assert_version(core, 0)
    finally:
        module.MAX_HISTORY_BYTES = old_limit
    link = core / "data/library.json"
    outside = root / "untouched.json"
    outside.write_bytes(b"untouched")
    link.unlink()
    try:
        link.symlink_to(outside)
    except OSError:
        link.write_bytes(payloads(0)["data/library.json"])
        print("SKIP: symlink creation unavailable on this platform/account")
    else:
        expect(PublicationError, lambda: store.commit(NAME, IDENT, payloads(1), {}))
        assert outside.read_bytes() == b"untouched"
        link.unlink()
        link.write_bytes(payloads(0)["data/library.json"])
    print("PASS: byte quota and link rejection preserve synthetic original files")


def test_thread_barrier(root):
    core = root / "threads"
    fixture(core)
    store = Store(core)
    acquired, release, complete = threading.Event(), threading.Event(), threading.Event()
    failures = []
    def writer():
        try:
            store.commit(NAME, IDENT, payloads(1), {})
            complete.set()
        except BaseException as exc:
            failures.append(repr(exc))
    def reader():
        with store.barrier():
            acquired.set()
            assert release.wait(10)
            assert_version(core, 0)
    a = threading.Thread(target=reader)
    b = threading.Thread(target=writer)
    a.start()
    assert acquired.wait(5)
    b.start()
    time.sleep(0.2)
    assert not complete.is_set()
    release.set()
    a.join(10)
    b.join(10)
    assert not a.is_alive() and not b.is_alive() and not failures and complete.is_set(), failures
    assert_version(core, 1)
    print("PASS: another thread cannot commit while a reader holds SH")


def test_process_barrier(root):
    core = root / "processes"
    fixture(core)
    ready, release = root / "lock-ready", root / "lock-release"
    process = subprocess.Popen([sys.executable, "-B", __file__, "--lock-child", "--core", str(core),
                                "--ready", str(ready), "--release", str(release)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert ready.exists(), "Child did not acquire shared barrier"
        done, errors = threading.Event(), []
        def writer():
            try:
                Store(core).commit(NAME, IDENT, payloads(1), {})
                done.set()
            except BaseException as exc:
                errors.append(repr(exc))
        thread = threading.Thread(target=writer)
        thread.start()
        time.sleep(.2)
        assert not done.is_set()
        release.write_bytes(b"release")
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stderr
        thread.join(15)
        assert not thread.is_alive() and not errors and done.is_set(), errors
        assert_version(core, 1)
    finally:
        if process.poll() is None:
            release.write_bytes(b"release")
            process.communicate(timeout=15)
    print("PASS: EX commit waits for a reader in another process")


def test_cleanup_interruption(root):
    core = root / "cleanup-interruption"
    fixture(core)
    store = Store(core)
    first, second = "pub_cleanup_0001", "pub_cleanup_0002"
    store.commit(NAME, first, payloads(1), {})
    store.commit(NAME, second, payloads(1), {})
    plan = store.preview_cleanup(NAME, [first])
    original_unlink = Path.unlink
    raised = []
    def interrupted(path, *args, **kwargs):
        if path.name == "manifest.json" and path.parent.name == first and not raised:
            raised.append(True)
            raise OSError("synthetic cleanup interruption")
        return original_unlink(path, *args, **kwargs)
    Path.unlink = interrupted
    try:
        expect(OSError, lambda: store.cleanup(NAME, [first], plan["digest"]))
    finally:
        Path.unlink = original_unlink
    Store(core).recover()
    Store(core).recover()
    assert not (store.root / "cleanup-active.json").exists()
    assert [r["publication_id"] for r in store.list_history(NAME)] == [second]
    assert store.cleanup(NAME, [first], plan["digest"])["already_cleaned"] is True
    assert_version(core, 1)
    print("PASS: interrupted exact-file cleanup resumes without touching current publication/data")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--lock-child", action="store_true")
    parser.add_argument("--core")
    parser.add_argument("--label", default="")
    parser.add_argument("--index", type=int, default=-1)
    parser.add_argument("--temp-root", help="Optional writable parent for synthetic temporary fixtures")
    parser.add_argument("--ready")
    parser.add_argument("--release")
    args = parser.parse_args()
    if args.child or args.lock_child:
        child(args)
        return
    parent = Path(args.temp_root).resolve() if args.temp_root else None
    if parent is not None and os.name == "nt" and not str(parent).startswith("\\\\?\\"):
        parent = Path("\\\\?\\" + str(parent))
    with tempfile.TemporaryDirectory(prefix="reader-publication-store-", dir=parent) as temporary:
        root = Path(temporary).resolve()
        if parent is not None:
            assert root.is_relative_to(parent) and root != parent
        test_crashes(root)
        test_recovery_refusal(root)
        test_pending_reader(root)
        test_aborted_binding(root)
        test_contracts_and_cleanup(root)
        test_limits_and_links(root)
        test_thread_barrier(root)
        test_process_barrier(root)
        test_cleanup_interruption(root)
    print("All publication-store checks passed; only synthetic temporary data was used.")


if __name__ == "__main__":
    main()
