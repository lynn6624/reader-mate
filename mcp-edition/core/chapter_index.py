"""Bounded, versioned TXT structure indexes. Every public coordinate is UTF-16.

TOC recognition combines a run of title-shaped lines, repeated later headings,
and a return to narrative text. No minimum chapter length or unique-title rule.
The cache retains indexes, never book text; old stored chapter labels are not indexes.
"""
import hashlib
import re
import threading
from bisect import bisect_right
from collections import OrderedDict
from dataclasses import dataclass

INDEX_VERSION = 2
_NUMBERED = re.compile(r'^(?:第\s*[0-9零〇一二三四五六七八九十百千万两壹贰叁肆伍陆柒捌玖拾]+\s*[章节回卷篇部幕集]|(?:chapter|part|book)\s+(?:\d+|[ivxlcdm]+)\b)', re.I)
_SUBSECTION = re.compile(r'^[甲乙丙丁戊己庚辛壬癸][、．.]\s*\S')
_FRONT = re.compile(r'^(?:译者序(?:言)?|(?:\d{4}年[^。！？；]{0,15})?[^。！？；]{0,12}(?:原序|序言)|前言|序章|楔子|引言|导言|绪论|后记|跋|尾声|终章|结语|结论|附录|致谢|出版说明)(?:\s.*|[（(].*[）)]|$)')
_PAGES = re.compile(r'\s*[.·…]{2,}\s*\d+\s*$')
_TOC = re.compile(r'^(?:目\s*录|contents|table of contents)$', re.I)
_cache = OrderedDict()
_cache_lock = threading.Lock()


@dataclass(slots=True)
class _Line:
    offset: int
    index: int
    number: int
    size: int
    title: str
    key: str
    kind: str


def _key(title):
    value = re.sub(r'\s+', ' ', _PAGES.sub('', title)).strip().casefold()
    return re.sub(r'(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])', '', value)


def _kind(title):
    plain = _PAGES.sub('', title).strip()
    if len(plain) > 100:
        return ''
    if _TOC.fullmatch(plain):
        return 'toc'
    if _NUMBERED.match(plain):
        return 'numbered'
    if _SUBSECTION.match(plain):
        return 'subsection'
    if _FRONT.match(plain):
        return 'frontmatter'
    return ''


def _title_shaped(row):
    return bool(row.title and row.size <= 100 and
                (row.kind or not re.search(r'[。！？!?；;]', row.title)))


def build_index(text):
    rows, total = [], 0
    for number, match in enumerate(re.finditer(r'[^\n]*(?:\n|$)', text)):
        raw = match.group()
        if not raw:
            continue
        title = raw.strip()
        if title:
            leading = len(raw) - len(raw.lstrip())
            short = title if len(title) <= 100 else ''
            rows.append(_Line(total + len(raw[:leading].encode('utf-16-le')) // 2,
                              match.start() + leading, number, len(title), short,
                              _key(short) if short else '', _kind(short) if short else ''))
        total += len(raw.encode('utf-16-le')) // 2
    occurrences = {}
    for i, row in enumerate(rows):
        if row.key:
            occurrences.setdefault(row.key, []).append(i)

    # Candidate runs are broken by prose, not by a fixed distance between headings.
    runs, i = [], 0
    while i < len(rows):
        if not _title_shaped(rows[i]):
            i += 1
            continue
        end = i + 1
        while end < len(rows) and _title_shaped(rows[end]):
            end += 1
        runs.append((i, end))
        i = end

    regions, toc_entries, excluded = [], [], set()
    for begin, end in runs:
        markers = [p for p in range(begin, end) if rows[p].kind == 'toc']
        start = markers[0] + 1 if markers else begin
        if end - start < 3:
            continue
        # The last title before prose can be the first actual body heading.
        # Keep it when it repeats an entry in the dense run; short chapters remain valid.
        body_start = end
        last = end - 1
        repeats_prefix = any(rows[p].key == rows[last].key for p in range(start, last))
        if end < len(rows) and repeats_prefix and rows[last].kind != 'toc':
            body_start = last
        entries = [p for p in range(start, body_start) if rows[p].kind != 'toc']
        if len(entries) < 3:
            continue
        typed = sum(bool(rows[p].kind) for p in entries)
        repeated = {rows[p].key for p in entries
                    if any(q >= body_start for q in occurrences.get(rows[p].key, []))}
        # Verify agreement with a later heading sequence, not the most distant duplicate.
        cursor, ordered = body_start - 1, 0
        for p in entries:
            later = occurrences.get(rows[p].key, [])
            at = bisect_right(later, cursor)
            if at < len(later):
                cursor = later[at]
                ordered += 1
        explicit = bool(markers)
        enough_structure = typed >= 3 and typed / len(entries) >= .4
        repeated_sequence = len(repeated) >= 3 and ordered >= 3
        if not enough_structure or not (repeated_sequence or (explicit and typed / len(entries) >= .7)):
            continue
        if not explicit and not (body_start < end and repeated_sequence):
            continue
        region_start = rows[markers[0]].offset if explicit else rows[start].offset
        # A title-only prelude belongs to the opening TOC region; narrative prefaces do not.
        prefix = rows[:markers[0] if explicit else start]
        if not prefix or (len(prefix) <= 6 and all(r.title and r.size <= 100 and
                            not re.search(r'[。；;]', r.title) for r in prefix)):
            region_start = 0
        region_end = rows[body_start].offset if body_start < len(rows) else total
        basis = 'explicit_toc+dense_titles+body_transition' if explicit else 'dense_repeated_sequence+body_transition'
        regions.append({'start':region_start, 'end':region_end, 'basis':basis})
        excluded.update(range(begin, body_start))
        for p in entries:
            row = rows[p]
            toc_entries.append({'title':_PAGES.sub('', row.title).strip(), 'start':row.offset,
                                'end':row.offset + len(row.title.encode('utf-16-le')) // 2,
                                'basis':basis, 'key':row.key})

    toc_keys = {entry['key'] for entry in toc_entries}
    headings, numbered_seen = [], False
    for i, row in enumerate(rows):
        if i in excluded or not row.title or row.kind == 'toc':
            continue
        referenced = row.key in toc_keys
        kind = row.kind
        # Generic prefaces/unnumbered TOC sections are structural only with context.
        if kind == 'numbered':
            if i and row.number == rows[i-1].number + 1 and re.match(r'^作者有话(?:要)?说[：:]', rows[i-1].title):
                continue  # An author's closing remark is not a new volume heading.
            numbered_seen = True
        elif kind == 'frontmatter':
            if numbered_seen and not referenced and not re.match(r'^(?:后记|跋|尾声|终章|结语|结论|附录|致谢)', row.title):
                continue
        elif not referenced:
            continue
        title = _PAGES.sub('', row.title).strip()
        headings.append({'title':title, 'start':row.offset, 'line':row.number,
                         'kind':kind or 'toc-referenced',
                         'basis':'body_heading+toc_match' if referenced else 'standalone_'+kind})
    for i, heading in enumerate(headings):
        heading['end'] = headings[i+1]['start'] if i+1 < len(headings) else total
    by_key = {}
    for heading in headings:
        by_key.setdefault(_key(heading['title']), []).append(heading['start'])
    for entry in toc_entries:
        entry['body_offsets'] = by_key.get(entry.pop('key'), [])
    return {'version':INDEX_VERSION, 'offset_unit':'utf16', 'total_units':total,
            'headings':headings, 'toc_regions':regions, 'toc_entries':toc_entries}


def index_for_text(text, revision=None):
    fingerprint = revision or 'text:' + hashlib.sha256(text.encode('utf-8')).hexdigest()
    key = (INDEX_VERSION, fingerprint)
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    result = build_index(text)
    with _cache_lock:
        if len(result['headings']) + len(result['toc_entries']) <= 20000:
            _cache[key] = result
            _cache.move_to_end(key)
            while len(_cache) > 2:
                _cache.popitem(last=False)
    return result


def chapter_at(index, offset):
    if not isinstance(offset, int) or isinstance(offset, bool) or not 0 <= offset <= index['total_units']:
        return {'title':'', 'region':'unresolved'}
    for region in index['toc_regions']:
        if region['start'] <= offset < region['end']:
            return {'title':'目录', 'region':'toc', 'start':region['start'], 'end':region['end']}
    headings = index['headings']
    lo, hi = 0, len(headings)
    while lo < hi:
        mid = (lo+hi)//2
        if headings[mid]['start'] <= offset:
            lo = mid+1
        else:
            hi = mid
    if lo:
        h = headings[lo-1]
        return {'title':h['title'], 'region':'body', 'start':h['start'], 'end':h['end']}
    return {'title':'', 'region':'unstructured'}


def public_index(index):
    return {'version':INDEX_VERSION, 'offset_unit':'utf16',
            'headings':[{key:h[key] for key in ('title','start','end')} for h in index['headings']],
            'toc_regions':[{key:r[key] for key in ('start','end')} for r in index['toc_regions']]}
