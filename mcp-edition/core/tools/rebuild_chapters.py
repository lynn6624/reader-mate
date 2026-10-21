#!/usr/bin/env python3
"""Preview/rebuild chapter metadata at existing UTF-16 positions, never re-save reading.

Apply/rollback require an offline Web service and an explicit backup directory.
Book bytes, offsets, revision counters, anchors and timestamps are never rewritten.
"""
import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import socket
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import reader_server as reader
from chapter_index import INDEX_VERSION, index_for_text, chapter_at


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def write_atomic(path, raw, template=None):
    path = Path(path)
    metadata = (Path(template).stat() if template else path.stat()) if (template or path.exists()) else None
    handle, temp = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(handle, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        if metadata:
            os.chmod(temp, metadata.st_mode & 0o777)
            if hasattr(os, 'chown') and os.geteuid() == 0:
                os.chown(temp, metadata.st_uid, metadata.st_gid)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, indent=1).encode('utf-8')


def require_offline(args):
    if not args.offline:
        raise ValueError('Apply/rollback requires --offline; stop the Web service first')
    try:
        connection = socket.create_connection(('127.0.0.1', args.port), timeout=.3)
    except OSError:
        return
    connection.close()
    raise ValueError('Web port is still listening; refuse concurrent migration')


def run(args):
    root = Path(args.root).resolve()
    progress_path, mirror_path = root / 'data/progress.json', root / 'data/阅读进度.md'
    if args.rollback:
        require_offline(args)
        backup = Path(args.rollback).resolve()
        manifest = json.loads((backup / 'manifest.json').read_text(encoding='utf-8'))
        if digest(progress_path.read_bytes()) != manifest['after_sha256']:
            raise ValueError('Progress changed since rebuild; refusing to overwrite newer reading data')
        if manifest.get('mirror_after_sha256') and mirror_path.exists() and digest(mirror_path.read_bytes()) != manifest['mirror_after_sha256']:
            raise ValueError('Progress mirror changed since rebuild; inspect before rollback')
        write_atomic(progress_path, (backup / 'progress.json').read_bytes(), progress_path)
        if (backup / 'reading-progress.md').exists():
            write_atomic(mirror_path, (backup / 'reading-progress.md').read_bytes(), mirror_path)
        elif mirror_path.exists():
            mirror_path.unlink()
        return {'rolled_back':True, 'offsets_revisions_times_preserved':True}
    if not args.all and not args.book:
        raise ValueError('Choose --all or one or more --book filenames')
    if args.apply:
        require_offline(args)
        if not args.backup_dir:
            raise ValueError('--apply requires --backup-dir (new private directory)')
    before = progress_path.read_bytes()
    document = json.loads(before.decode('utf-8-sig'))
    if not isinstance(document, dict) or not isinstance(document.get('books'), dict):
        raise ValueError('Invalid progress store; refusing to replace or normalize it')
    changed = copy.deepcopy(document)
    names = sorted(document['books']) if args.all else list(dict.fromkeys(args.book))
    report = {'index_version':INDEX_VERSION, 'offset_unit':'utf16', 'applied':False,
              'changes':[], 'skipped':[], 'books':[], 'before_sha256':digest(before)}
    exported = {}
    for name in names:
        if reader.safe_name(name) != name:
            raise ValueError('Invalid exact book filename')
        path = root / '书库' / name
        if not path.is_file() or path.is_symlink():
            report['skipped'].append({'name':name,'reason':'missing_or_symlink_book'})
            continue
        text, revision = reader.book_snapshot(str(path))
        index = index_for_text(text, revision)
        if args.export_index:
            exported[name] = index
        report['books'].append({'name':name,'toc_entries':len(index['toc_entries']),
                                'body_starts':len(index['headings']),'book_revision':revision})
        original_book = document['books'].get(name, {})
        if not isinstance(original_book, dict):
            report['skipped'].append({'name':name,'reason':'invalid_legacy_record'})
            continue
        flat = any(k in original_book for k in ('offset','percent','anchor','updated','chapter'))
        records = {'user':original_book} if flat else original_book
        for who, record in records.items():
            if not isinstance(record, dict):
                continue
            def skip(reason):
                report['skipped'].append({'name':name,'reader_id':who,'reason':reason})
            raw_offset = record.get('offset')
            if raw_offset is None or isinstance(raw_offset, bool):
                skip('no_persisted_offset'); continue
            try:
                offset = int(raw_offset)
                if isinstance(raw_offset, float) and raw_offset != offset:
                    raise ValueError('fractional offset')
            except (ValueError, TypeError):
                skip('invalid_offset'); continue
            position = reader.text_position(text, offset)
            if position is None:
                skip('invalid_utf16_position'); continue
            if record.get('book_revision') and record['book_revision'] != revision:
                skip('content_changed'); continue
            if any(k in record and record[k] != expected for k,expected in
                   (('paragraph_idx',position[0]),('char_offset',position[1]))):
                skip('coordinate_conflict'); continue
            anchor = record.get('anchor')
            if anchor is not None and not isinstance(anchor, str):
                skip('invalid_anchor'); continue
            if not record.get('book_revision') and anchor:
                normalized = lambda s: re.sub(r'\s+', ' ', s).strip()
                if normalized(anchor) != normalized(text[position[3]:position[3]+40]):
                    skip('legacy_anchor_unverified'); continue
            found = chapter_at(index, offset)
            replacement = dict(record, chapter=found['title'], chapter_index_version=INDEX_VERSION,
                               chapter_location_status=found['region'])
            if replacement == record:
                continue
            report['changes'].append({'name':name,'reader_id':who,'offset':raw_offset,
                'before_chapter':record.get('chapter'), 'after_chapter':found['title'],
                'chapter_location_status':found['region']})
            if flat:
                changed['books'][name] = replacement
            else:
                changed['books'][name][who] = replacement
    if args.export_index:
        write_atomic(Path(args.export_index).resolve(), encoded(exported))
    if args.apply and changed != document:
        backup = Path(args.backup_dir).resolve()
        backup.mkdir(mode=0o700, parents=True, exist_ok=False)
        shutil.copy2(progress_path, backup / 'progress.json')
        if mirror_path.exists():
            shutil.copy2(mirror_path, backup / 'reading-progress.md')
        if digest(progress_path.read_bytes()) != digest(before):
            raise ValueError('Progress changed during preview; refusing to overwrite it')
        for book in report['books']:
            if reader.file_revision(str(root / '书库' / book['name'])) != book['book_revision']:
                raise ValueError('Book content changed during preview; refusing stale chapter metadata')
        after = encoded(changed)
        manifest = {'before_sha256':digest(before),'after_sha256':digest(after),
                    'index_version':INDEX_VERSION}
        write_atomic(backup / 'manifest.json', encoded(manifest))
        write_atomic(progress_path, after)
        old_mirror = reader.MD_FILE
        try:
            reader.MD_FILE = str(mirror_path)
            mirror = copy.deepcopy(changed)
            mirror['books'] = {n:reader.book_progress(v) for n,v in changed['books'].items()}
            library = reader.load_json(str(root / 'data/library.json'), {})
            reader.write_progress_md(mirror, library)
            # The maintenance command may run as root; retain the old mirror's owner/mode.
            template = backup / 'reading-progress.md'
            if template.exists():
                write_atomic(mirror_path, mirror_path.read_bytes(), template)
        except Exception:
            write_atomic(progress_path, before)
            if (backup / 'reading-progress.md').exists():
                write_atomic(mirror_path, (backup / 'reading-progress.md').read_bytes())
            raise
        finally:
            reader.MD_FILE = old_mirror
        manifest['mirror_after_sha256'] = digest(mirror_path.read_bytes())
        write_atomic(backup / 'manifest.json', encoded(manifest))
        report.update(applied=True, backup_dir=str(backup), after_sha256=digest(after))
    elif args.apply:
        report['already_current'] = True
    if args.report:
        write_atomic(Path(args.report).resolve(), encoded(report))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default=str(ROOT))
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--all', action='store_true')
    group.add_argument('--book', action='append')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--port', type=int, default=int(os.environ.get('READER_PORT','8831')))
    parser.add_argument('--backup-dir')
    parser.add_argument('--rollback')
    parser.add_argument('--report')
    parser.add_argument('--export-index')
    result = run(parser.parse_args())
    print(json.dumps({'applied':result.get('applied',False), 'changed':len(result.get('changes',[])),
                      'skipped':len(result.get('skipped',[])), 'already_current':result.get('already_current',False),
                      'rolled_back':result.get('rolled_back',False)}, ensure_ascii=True))


if __name__ == '__main__':
    main()
