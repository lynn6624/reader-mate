"""Navigation metadata only. Never rewrite book bytes or reading records."""
import hashlib
import json
import re
from pathlib import Path
from chapter_index import index_for_text

SCHEMA = 1
MAX_ITEMS = 8000
MAX_DEPTH = 8
WINDOW_UNITS = 60000
_NUMBER = re.compile(r'^(?:第\s*[0-9零〇一二三四五六七八九十百千万两]+\s*[章节回卷篇部]|(?:chapter|part|book)\s+[0-9ivxlcdm]+\b)', re.I)
_BARE = re.compile(r'^(?:[一二三四五六七八九十百]{1,5}|\d{1,3})$')
_FRONT = re.compile(r'^(?:序$|译者序|前言|序言|引言|导言|绪论|后记|跋$|附录|致谢|尾声|终章|结语|结论|\d{4}年.*序言)')


def metadata_path(root, name):
    return Path(root) / 'navigation' / (hashlib.sha256(name.encode('utf-8')).hexdigest()+'.json')


def quote_at(raw, offset):
    return raw[offset*2:(offset+40)*2].decode('utf-16-le',errors='ignore')


def valid_offset(raw, offset):
    if not isinstance(offset,int) or isinstance(offset,bool) or not 0 <= offset < len(raw)//2:
        return False
    unit = int.from_bytes(raw[offset*2:offset*2+2],'little')
    return not 0xDC00 <= unit <= 0xDFFF


def _rows(text):
    rows=[]; offset=0
    for line in text.splitlines(keepends=True):
        title=line.strip(); lead=len(line)-len(line.lstrip())
        if title:
            rows.append(dict(title=title if len(title)<=100 else '',
                             offset=offset+len(line[:lead].encode('utf-16-le'))//2,
                             prose=len(title)>100 or bool(re.search(r'[。！？；!?;]',title))))
        offset+=len(line.encode('utf-16-le'))//2
    return rows


def automatic(text, revision):
    base=index_for_text(text,revision)
    rows=_rows(text); total=base['total_units']; regions=[dict(r) for r in base['toc_regions']]
    markers=[i for i,row in enumerate(rows) if re.fullmatch(r'目\s*录|contents|table of contents',row['title'],re.I)]
    local=[]; opening_entries=None
    # Some anthologies have a separate short TOC for every work, with no numbered titles.
    for number,marker in enumerate(markers):
        stop=markers[number+1] if number+1<len(markers) else len(rows)
        entries=[]; seen=set(); body=None; end=marker+1
        for i in range(marker+1,min(stop,marker+180)):
            row=rows[i]
            if row['prose']:
                end=i;break
            title=row['title']
            if not title:break
            # Repeat must introduce prose; duplicate links in the TOC itself are not body.
            if entries and title==entries[0] and len(entries)>=3 and any(r['prose'] for r in rows[i+1:min(i+5,stop)]):
                body=i;end=i;break
            entries.append(title);seen.add(title);end=i+1
        if len(entries)<3:continue
        if number==0:opening_entries=list(entries)
        if body is None and end==min(stop,marker+180):
            # An unbounded title list is uncertain; do not erase real headings.
            if end!=stop:continue
        boundary=rows[end]['offset'] if end<len(rows) else total
        if body is not None:
            for region in regions:
                if region['start']<=rows[marker]['offset']<region['end']:
                    region['end']=min(region['end'],boundary)
        regions.append(dict(start=rows[marker]['offset'],end=boundary))
        if body is not None:
            local.append(dict(body=body,stop=stop,entries=entries))
    def in_toc(offset):return any(r['start']<=offset<r['end'] for r in regions)
    found={h['start']:dict(title=h['title'],offset=h['start'],basis=h['basis'])
           for h in base['headings'] if not in_toc(h['start'])}
    works=[]
    for section in local:
        body,stop=section['body'],section['stop']
        first=rows[body];title=first['title']
        is_work=not _NUMBER.match(title) and not _FRONT.match(title) and len(title)<=60
        section_end=rows[stop]['offset'] if stop<len(rows) else total
        cursor=body
        for entry_number,title in enumerate(section['entries']):
            matched=next((i for i in range(cursor,stop) if rows[i]['title']==title and not in_toc(rows[i]['offset'])),None)
            if matched is None:continue
            row=rows[matched];cursor=matched+1
            found[row['offset']]=dict(title=title,offset=row['offset'],basis='local_toc+body_match')
            following=section['entries'][entry_number+1] if entry_number+1<len(section['entries']) else ''
            work_candidate=matched==body or bool(_NUMBER.match(following)) or following in ('一','1')
            if is_work and work_candidate and not _NUMBER.match(title) and not _BARE.fullmatch(title) and not _FRONT.match(title):
                works.append((row['offset'],section_end))
        # Bare chapter numbers are accepted only inside a confirmed work, in a series starting at 一/1.
        bare=[r for r in rows[body+1:stop] if _BARE.fullmatch(r['title']) and not in_toc(r['offset'])]
        if is_work and len(bare)>=2 and bare[0]['title'] in ('一','1'):
            for row in bare:
                found.setdefault(row['offset'],dict(title=row['title'],offset=row['offset'],basis='work+bare_chapter_series'))
    # A standalone 序 is valid frontmatter; don't promote every unnumbered short line.
    first_numbered=min((r['offset'] for r in rows if _NUMBER.match(r['title']) and not in_toc(r['offset'])),default=total)
    for row in rows:
        if row['title']=='序' and row['offset']<first_numbered and not in_toc(row['offset']):
            found.setdefault(row['offset'],dict(title='序',offset=row['offset'],basis='standalone_preface'))
    # A list consisting only of headings is not evidence of chapter bodies.
    ordered=sorted(found)
    nonhead=[r['offset'] for r in rows if r['offset'] not in found and not in_toc(r['offset'])]
    from bisect import bisect_right,bisect_left
    valid=set()
    for i,off in enumerate(ordered):
        end=ordered[i+1] if i+1<len(ordered) else total
        if bisect_right(nonhead,off)<bisect_left(nonhead,end):valid.add(off)
    # Structural parents may have no prose of their own, immediately preceding a child.
    for i in range(len(ordered)-1,-1,-1):
        off=ordered[i];title=found[off]['title']
        parent=any(start==off for start,end in works) or bool(re.match(r'^第.+[篇部卷]|^(?:part|book)\s',title,re.I))
        if parent and i+1<len(ordered) and ordered[i+1] in valid:valid.add(off)
    found={off:value for off,value in found.items() if off in valid}
    virtual=[]
    # A collection's first work sometimes has no body book-title line. A leading
    # collection TOC followed by a local TOC repeating >=3 of its chapter labels
    # can bind that work label to its first *body* chapter, never to either TOC.
    if opening_entries and local:
        label=opening_entries[0];section=local[0];off=rows[section['body']]['offset']
        shared=set(opening_entries).intersection(section['entries'])
        known_work=any(found.get(start,{}).get('title')==label for start,end in works)
        if not known_work and off in found and len(shared)>=3 and not _NUMBER.match(label) and not _FRONT.match(label):
            sequence=[title for title in section['entries'] if title in shared]
            cursor=-1;ordered_match=True
            for title in sequence:
                candidate=next((i for i in range(cursor+1,len(opening_entries)) if opening_entries[i]==title),None)
                if candidate is None:ordered_match=False;break
                cursor=candidate
            if ordered_match:
                end=rows[section['stop']]['offset'] if section['stop']<len(rows) else total
                works.append((off,end))
                virtual.append(dict(title=label,offset=off,basis='collection_toc+local_body_sequence',virtual=True))
    works=sorted(set(works))
    works=[(start,min(end,works[i+1][0]) if i+1<len(works) else end) for i,(start,end) in enumerate(works)]
    items=[]; part=None; raw=text.encode('utf-16-le'); work_parent=None
    events=sorted(list(found.values())+virtual,key=lambda x:(x['offset'],not x.get('virtual',False)))
    for row in events:
        off=row['offset']; work=next((pair for pair in works if pair[0]<=off<pair[1]),None)
        if work_parent and (not work or work_parent[0]!=work[0]):work_parent=None;part=None
        parent=None
        if row.get('virtual') or (work and off==work[0] and not (work_parent and work_parent[0]==off)):
            kind='work';part=None
        elif re.match(r'^第.+[篇部卷]|^(?:part|book)\s',row['title'],re.I):
            kind='part';parent=work_parent[1] if work_parent else None
        else:
            kind='chapter';parent=part or (work_parent[1] if work_parent else None)
        ident='n_'+hashlib.sha256((str(off)+'\0'+row['title']).encode()).hexdigest()[:18]
        clean={key:value for key,value in row.items() if key!='virtual'}
        items.append(dict(clean,id=ident,parent_id=parent,order=len(items),kind=kind,quote=quote_at(raw,off)))
        if kind=='work':work_parent=(off,ident)
        elif kind=='part':part=ident
        if len(items)>=MAX_ITEMS:break
    return dict(schema_version=SCHEMA,book_revision=revision,revision=0,source='automatic',
                status='ok' if items else 'no_reliable_headings',items=items,
                toc_regions=[dict(start=r['start'],end=r['end']) for r in regions],
                truncated=len(events)>MAX_ITEMS)


def get(root, name, text, revision):
    path=metadata_path(root,name)
    if path.exists():
        try:
            value=json.loads(path.read_text(encoding='utf-8'))
            if value.get('schema_version')!=SCHEMA or not isinstance(value.get('items'),list):raise ValueError()
        except (ValueError,TypeError):
            return dict(schema_version=SCHEMA,revision=0,book_revision=revision,source='saved',status='invalid_metadata',items=[])
        if value.get('book_revision')!=revision:
            return dict(schema_version=SCHEMA,revision=value.get('revision',0),book_revision=revision,
                        source=value.get('source'),status='content_changed',items=[])
        try:validate(value['items'],text)
        except (ValueError,TypeError,KeyError):
            return dict(schema_version=SCHEMA,revision=value.get('revision',0),book_revision=revision,
                        source=value.get('source'),status='invalid_metadata',items=[])
        return dict(value,status='ok' if value['items'] else 'no_reliable_headings')
    return automatic(text,revision)


def validate(items,text):
    if not isinstance(items,list) or len(items)>MAX_ITEMS:raise ValueError('目录最多8000项，items必须为数组')
    raw=text.encode('utf-16-le'); output=[]; depths={}; seen=set()
    for index,item in enumerate(items):
        if not isinstance(item,dict):raise ValueError('目录项必须是对象')
        ident=item.get('id');title=item.get('title');off=item.get('offset');parent=item.get('parent_id')
        if not isinstance(ident,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',ident) or ident in seen:raise ValueError('目录ID无效或重复')
        if not isinstance(title,str) or not title.strip() or len(title)>160 or re.search(r'[\x00-\x1f]',title):raise ValueError('目录标题无效')
        if not valid_offset(raw,off):raise ValueError('目录偏移越界或位于emoji中间')
        if parent is not None and (not isinstance(parent,str) or parent not in seen):raise ValueError('父项必须存在且排在子项之前，禁止循环层级')
        depth=depths[parent]+1 if parent else 0
        if depth>=MAX_DEPTH:raise ValueError('目录层级最多8层')
        if not isinstance(item.get('quote'),str) or not item['quote'] or item['quote']!=quote_at(raw,off):raise ValueError('目录定位原文quote不匹配，请核对正文和偏移')
        seen.add(ident);depths[ident]=depth
        output.append(dict(id=ident,title=title.strip(),offset=off,parent_id=parent,order=index,
                           quote=quote_at(raw,off),basis='administrator_verified'))
    return output


def preview_digest(name,revision,expected,items):
    raw=json.dumps([name,revision,expected,items],ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    return hashlib.sha256(raw).hexdigest()


def reading_window(text,offset):
    raw=text.encode('utf-16-le');total=len(raw)//2
    if offset==total:offset=max(0,total-1)
    while offset and not valid_offset(raw,offset):offset-=1
    start=max(0,offset-8000);end=min(total,offset+WINDOW_UNITS-8000)
    while start and not valid_offset(raw,start):start-=1
    while end<total and not valid_offset(raw,end):end-=1
    # Prefer whole line boundaries when nearby, but cap even a single enormous paragraph.
    prefix=raw[start*2:offset*2].decode('utf-16-le');newline=prefix.find('\n')
    if start>0 and 0<=newline<2048:start+=len(prefix[:newline+1].encode('utf-16-le'))//2
    tail=raw[offset*2:end*2].decode('utf-16-le');newline=tail.rfind('\n')
    if end<total and newline>=0:
        candidate=offset+len(tail[:newline+1].encode('utf-16-le'))//2
        if candidate>=end-2048:end=candidate
    return dict(text=raw[start*2:end*2].decode('utf-16-le'),text_base=start,window_start=start,
                window_end=end,total_units=total,windowed=True,more_before=start>0,more_after=end<total)
