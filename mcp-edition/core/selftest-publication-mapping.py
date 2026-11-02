"""Pure synthetic mapping tests: no file fixtures, network, browser or real data."""
import copy
from dataclasses import FrozenInstanceError

import book_drafts as drafts
import publication_mapping as kernel


COUNT = 0


def check(condition, detail=None):
    global COUNT
    COUNT += 1
    assert condition, detail


def op(ident, start, end, before, after):
    return dict(id=ident, start=start, end=end, before=before, after=after)


def build(text, operations=(), **extra):
    args = dict(base_revision='snapshot-v1', current_revision='snapshot-v1', source_size_bytes=len(text.encode('utf-8')))
    args.update(extra)
    return kernel.build_mapping(text, operations, **args)


def plan(text, operations=()):
    result = build(text, operations)
    check(result['ok'], result['issues'])
    return result['mapping']


def code(result, expected):
    check(not result['ok'], result)
    check(result['issues'][0]['code'] == expected, result)


def test_replay():
    text = '甲😀乙丙丁'
    # Second edit is in the intermediate text; sorting would apply the wrong edit.
    operations = [op('later-position-first', 4, 5, '丙', '戊戊'),
                  op('earlier-position-second', 0, 1, '甲', '己')]
    original = copy.deepcopy(operations)
    mapping = plan(text, operations)
    check(mapping.target_text == '己😀乙戊戊丁')
    check([x.id for x in mapping.operations] == [x['id'] for x in operations])
    check(operations == original)
    operations[0]['after'] = 'caller changed its object'
    check(mapping.target_text == '己😀乙戊戊丁' and mapping.operations[0].after == '戊戊')
    try:
        mapping.target_text = 'mutate'
    except FrozenInstanceError:
        pass
    else:
        raise AssertionError('mapping was mutable')
    code(build(text, [], base_revision='stale-v0'), 'base_revision_mismatch')
    code(build(text, [op('bad-before', 0, 1, '乙', '己')]), 'operation_before_mismatch')
    code(build(text, [op('split-emoji', 2, 3, '', '己')]), 'invalid_operation_span')
    code(build(text, [op('boolean-position', True, 1, '', '己')]), 'invalid_operation_span')
    code(build(text, [op('negative-position', -1, 0, '', '己')]), 'invalid_operation_span')
    code(build(text, [op('past-eof', 7, 7, '', '己')]), 'invalid_operation_span')
    code(build(text, [op('bad-unicode', 0, 0, '', '\ud800')]), 'invalid_operation_unicode')
    code(build(text, [op('same', 0, 0, '', '己'), op('same', 0, 0, '', '己')]), 'duplicate_operation_id')
    code(build(text, [op('noop', 0, 1, '甲', '甲')]), 'noop_operation')
    code(build(text, [], source_size_bytes=drafts.MAX_SOURCE_BYTES + 1), 'source_byte_limit')
    code(build(text, [], source_size_bytes=True), 'source_byte_limit')
    code(build('a' * (drafts.MAX_UNITS + 1)), 'source_unit_limit')
    code(build(text, [op('wide', 0, 0, '', 'a' * 4001)]), 'selection_limit')
    code(build(text, [op(str(i), 0, 0, '', 'a') for i in range(201)]), 'operation_limit')
    check(build(text, [op(str(i), 0, 0, '', 'a') for i in range(200)])['ok'])
    code(build('a' * drafts.MAX_UNITS, [op('overflow-target', 0, 0, '', 'x')]), 'target_unit_limit')
    mapping = plan('abcd', [op('insert', 1, 1, '', 'X'), op('edit-inserted', 1, 2, 'X', 'YZ')])
    check(mapping.target_text == 'aYZbcd')
    check(kernel.map_range(mapping, 1, 3, 'bc', record_id='chain')['target']['start'] == 3)
    mapping = plan('abcd', [op('expand', 0, 1, 'a', 'AA'), op('shifted-coordinate', 3, 4, 'c', 'C')])
    check(mapping.target_text == 'AAbCd')
    code(build('abcd', [op('expand', 0, 1, 'a', 'AA'), op('incorrect-old-coordinate', 2, 3, 'c', 'C')]), 'operation_before_mismatch')


def test_ranges():
    base = 'abcdef'
    start = plan(base, [op('at-start', 2, 2, '', '😀')])
    result = kernel.map_range(start, 2, 4, 'cd', record_id='start-right')
    check(result['ok'] and result['target'] == dict(start=4, end=6, quote='cd'), result)
    end = plan(base, [op('at-end', 4, 4, '', '😀')])
    result = kernel.map_range(end, 2, 4, 'cd', record_id='end-left')
    check(result['ok'] and result['target']['end'] == 4, result)
    inside = plan(base, [op('inside', 3, 3, '', 'X')])
    result = kernel.map_range(inside, 2, 4, 'cd', record_id='internal')
    code(result, 'insertion_inside_range')
    check(result['issues'][0]['operation_id'] == 'inside' and result['issues'][0]['record_id'] == 'internal')
    for ident, a, b, before, after, overlap in (
            ('partial-delete', 1, 3, 'bc', '', 'partial'),
            ('whole-delete', 2, 4, 'cd', '', 'whole'),
            ('enclosing-delete', 1, 5, 'bcde', '', 'whole'),
            ('internal-delete', 3, 4, 'd', '', 'partial'),
            ('replace-even-same-length', 2, 4, 'cd', 'XY', 'whole')):
        result = kernel.map_range(plan(base, [op(ident, a, b, before, after)]), 2, 4, 'cd', record_id=ident)
        code(result, 'range_overlap')
        check(result['issues'][0]['overlap'] == overlap, result)
    before = plan(base, [op('adjacent-before', 0, 2, 'ab', '')])
    check(kernel.map_range(before, 2, 4, 'cd', record_id='adjacent')['target']['start'] == 0)
    after = plan(base, [op('adjacent-after', 4, 6, 'ef', '')])
    check(kernel.map_range(after, 2, 4, 'cd', record_id='adjacent')['target']['end'] == 4)
    code(kernel.map_range(inside, 2, 4, 'WRONG', record_id='old-first'), 'old_quote_mismatch')
    code(kernel.map_range(start, 2, 2, '', record_id='empty'), 'invalid_range')
    code(kernel.map_range(start, True, 4, 'bcd', record_id='bool'), 'invalid_range')
    code(kernel.map_range(start, 2, 4, 'cd', record_id=''), 'invalid_record_id')
    code(kernel.map_range(None, 2, 4, 'cd', record_id='missing-plan'), 'invalid_mapping')
    repeated = plan('同句甲。同句乙。', [op('delete-first-copy', 0, 4, '同句甲。', '')])
    result = kernel.map_range(repeated, 4, 6, '同句', record_id='second-copy')
    check(result['ok'] and result['target']['start'] == 0)
    code(kernel.map_range(repeated, 1, 3, '同句', record_id='never-find-first'), 'old_quote_mismatch')
    code(kernel.map_range(repeated, 0, 2, '同句', record_id='deleted-first-copy'), 'range_overlap')
    emoji = plan('a😀bc', [op('before-emoji', 0, 0, '', '另')])
    check(kernel.map_range(emoji, 1, 3, '😀', record_id='whole-emoji')['target']['start'] == 2)
    code(kernel.map_range(emoji, 2, 3, '😀', record_id='half-emoji'), 'old_quote_mismatch')


def test_points():
    mapping = plan('abcd', [op('insert-boundary', 1, 1, '', '😀')])
    check(kernel.map_point(mapping, 1, 'b', record_id='right')['target']['offset'] == 3)
    code(kernel.map_point(mapping, 1, 'b', record_id='left-new-check', affinity='left'), 'new_quote_mismatch')
    code(kernel.map_point(mapping, 0, 'ab', record_id='anchor-interior'), 'point_quote_changed')
    code(kernel.map_point(mapping, 0, 'wrong', record_id='old-first'), 'old_quote_mismatch')
    code(kernel.map_point(mapping, 0, '', record_id='no-evidence'), 'old_quote_mismatch')
    code(kernel.map_point(mapping, True, 'b', record_id='bool'), 'old_quote_mismatch')
    code(kernel.map_point(mapping, -1, '', record_id='negative'), 'old_quote_mismatch')
    code(kernel.map_point(mapping, 5, '', record_id='past-eof'), 'old_quote_mismatch')
    code(kernel.map_point(mapping, 1, 'b', record_id='bad-affinity', affinity='nearest'), 'invalid_affinity')
    code(kernel.map_point(mapping, 1, 'b', record_id=None), 'invalid_record_id')
    code(kernel.map_point(None, 1, 'b', record_id='missing-plan'), 'invalid_mapping')
    removed = plan('abcd', [op('remove-bc', 1, 3, 'bc', '')])
    code(kernel.map_point(removed, 1, 'b', record_id='at-deletion-start'), 'point_overlap')
    code(kernel.map_point(removed, 2, 'c', record_id='inside-deletion'), 'point_overlap')
    check(kernel.map_point(removed, 3, 'd', record_id='at-deletion-end')['target']['offset'] == 1)
    replaced = plan('abcd', [op('replace-b', 1, 2, 'b', 'BB')])
    code(kernel.map_point(replaced, 1, 'b', record_id='replaced'), 'point_overlap')
    code(kernel.map_point(replaced, 0, 'ab', record_id='quote-replaced'), 'point_quote_changed')
    appended = plan('abcd', [op('append', 4, 4, '', '尾')])
    check(kernel.map_point(appended, 4, '', record_id='eof-right')['target']['offset'] == 5)
    check(kernel.map_point(appended, 4, '', record_id='eof-left', affinity='left')['target']['offset'] == 4)
    check(kernel.map_range(appended, 0, 4, 'abcd', record_id='range-eof')['target']['end'] == 4)
    emoji = plan('a😀b')
    code(kernel.map_point(emoji, 2, '', record_id='half-emoji'), 'old_quote_mismatch')
    check(kernel.map_point(emoji, 4, '', record_id='emoji-eof')['ok'])
    empty = plan('')
    check(kernel.map_point(empty, 0, '', record_id='empty-book-eof')['ok'])
    code(kernel.map_range(empty, 0, 0, '', record_id='empty-book-range'), 'invalid_range')


def test_many_identities_and_no_authentication():
    identities = ['user', '004', 'member_new', 'retired_agent', 'unknown.id', 'offset', 'updated']
    text = ''.join(f'第{i}句甲😀乙。\n' for i in range(28))
    mapping = plan(text, [op('prefix', 0, 0, '', '前置😀\n')])
    records = []
    for i in range(28):
        quote = f'第{i}句甲😀乙。'
        start = drafts.units(text[:text.index(quote)])
        row = dict(id='history-' + str(i), author_id=identities[i % len(identities)],
                   revision=0, member_status='unknown-or-revoked', extra={'keep': [i]}, quote=quote)
        if i % 2:
            row.update(kind='point', offset=start)
        else:
            row.update(kind='range', start=start, end=start + drafts.units(quote))
        records.append(row)
    original = copy.deepcopy(records)
    report = kernel.map_records(mapping, records)
    check(report['ok'] and len(report['records']) == 28, report)
    shift = drafts.units('前置😀\n')
    for row, result in zip(records, report['records']):
        key = 'offset' if row['kind'] == 'point' else 'start'
        check(result['target'][key] == row[key] + shift)
        check('legacy_verified' not in result and 'author_id' not in result and 'revision' not in result)
    check(records == original)
    # A mechanically valid position is not proof that a revision=0 record is a save.
    unproven = dict(id='unproven', kind='point', offset=0, quote=text[0], revision=0)
    check(kernel.map_records(mapping, [unproven])['ok'])
    unchanged = plan(text)
    check(unchanged.target_text == text and not unchanged.operations)
    for row, result in zip(records, kernel.map_records(unchanged, records)['records']):
        key = 'offset' if row['kind'] == 'point' else 'start'
        check(result['target'][key] == row[key])
    broken = copy.deepcopy(records)
    broken[5]['quote'] = 'wrong'
    report = kernel.map_records(mapping, broken)
    check(not report['ok'] and len(report['records']) == 28)
    check(report['issues'][0]['record_id'] == 'history-5')
    code(kernel.map_records(mapping, [records[0], records[0]]), 'invalid_record_id')
    code(kernel.map_records(mapping, None), 'invalid_records')
    code(kernel.map_records(None, []), 'invalid_mapping')


if __name__ == '__main__':
    test_replay()
    test_ranges()
    test_points()
    test_many_identities_and_no_authentication()
    print(f'PASS publication mapping: {COUNT} assertions; 28 multi-identity records; ordered UTF-16 replay, version/before/limits, quote checks, insertion affinities, deletion/replacement refusal, emoji, duplicates, EOF, immutable inputs; no files/network/authentication/commit')
