"""Bounded, revision-bound draft operations. Never write formal text or notes."""
import copy
import hashlib
import json
import os
from pathlib import Path
import secrets
import tempfile
from datetime import datetime, timezone
import navigation as nav

MAX_UNITS = 2_000_000
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_FILE = 4 * 1024 * 1024
MAX_EDITS = 200
MAX_SELECTION = 4000


class DraftError(ValueError):
    def __init__(self, message, status=409):
        super().__init__(message); self.status = status


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def path_for(root, name):
    return Path(root) / 'drafts' / (hashlib.sha256(name.encode()).hexdigest()+'.json')


def units(text):
    return len(text.encode('utf-16-le'))//2


def span(raw, start, end):
    if type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(raw)//2:
        raise DraftError('选区位置无效',400)
    try:
        raw[:start*2].decode('utf-16-le');raw[end*2:].decode('utf-16-le')
        return raw[start*2:end*2].decode('utf-16-le')
    except UnicodeError as exc:
        raise DraftError('选区不能截断 emoji',400) from exc


def read_metadata(root, name):
    """Read the bounded draft record, including approval receipts, without replay."""
    path=path_for(root,name)
    if not path.exists():return None
    try:
        with path.open('rb') as f:raw=f.read(MAX_FILE+1)
        if len(raw)>MAX_FILE:raise ValueError()
        value=json.loads(raw.decode('utf-8'),object_pairs_hook=_pairs)
        if not isinstance(value,dict):raise ValueError()
        if (value.get('schema_version')!=1 or value.get('name')!=name or type(value.get('revision')) is not int
                or value['revision']<1 or not isinstance(value.get('operations'),list) or len(value['operations'])>MAX_EDITS
                or not isinstance(value.get('chapter_issues'),list)):
            raise ValueError()
        return value
    except (OSError,ValueError,KeyError,TypeError,UnicodeError) as exc:
        raise DraftError('草稿记录损坏，原文件已保留；请检查备份',503) from exc


def load(root, name, text, revision):
    value=read_metadata(root,name)
    if value is None:return None,text
    try:
        if value.get('base_revision')!=revision:
            raise DraftError('原书已变化，旧草稿已保留；请管理员核对，不能自动套用')
        raw=text.encode('utf-16-le');ids=set()
        for op in value['operations']:
            if op['id'] in ids or not isinstance(op['after'],str) or not isinstance(op['before'],str):raise ValueError()
            ids.add(op['id'])
            if units(op['before'])>MAX_SELECTION or units(op['after'])>MAX_SELECTION:raise ValueError()
            if span(raw,op['start'],op['end'])!=op['before']:raise ValueError()
            raw=raw[:op['start']*2]+op['after'].encode('utf-16-le')+raw[op['end']*2:]
            if len(raw)//2>MAX_UNITS:raise ValueError()
        result=raw.decode('utf-16-le')
        nav.validate(value['items'],result)
        return value,result
    except DraftError:raise
    except (OSError,ValueError,KeyError,TypeError,UnicodeError) as exc:
        raise DraftError('草稿记录损坏，原文件已保留；请检查备份',503) from exc


def _pairs(pairs):
    result={}
    for k,v in pairs:
        if k in result:raise ValueError('duplicate key')
        result[k]=v
    return result


def create(name,text,revision,table):
    if units(text)>MAX_UNITS:raise DraftError('本阶段草稿最多200万 UTF-16 单位；此书超限',413)
    if table.get('status') in ('invalid_metadata','content_changed'):
        raise DraftError('请先核对原目录，再建立草稿')
    return dict(schema_version=1,name=name,base_revision=revision,revision=1,created=stamp(),updated=stamp(),
        updated_by='user',operations=[],items=copy.deepcopy(table['items']),chapter_issues=[],cursor_offset=0)


def check_version(value,data):
    if data.get('base_revision')!=value['base_revision'] or type(data.get('expected_revision')) is not int or data['expected_revision']!=value['revision']:
        raise DraftError('草稿已在别处更新，请保留输入并重新打开核对')


def edit(value,text,data):
    ident=data.get('request_id')
    if not isinstance(ident,str) or not 8<=len(ident)<=80 or not all(c.isalnum() or c in '_-' for c in ident):
        raise DraftError('编辑请求标识无效',400)
    fingerprint=hashlib.sha256(json.dumps({k:data.get(k) for k in ('base_revision','expected_revision','start','end','quote','replacement')},ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    prior=next((op for op in value['operations'] if op['id']==ident),None)
    if prior:
        if prior.get('request_digest')!=fingerprint:raise DraftError('同一请求标识不能用于不同编辑')
        return value,text,True
    check_version(value,data)
    if len(value['operations'])>=MAX_EDITS:raise DraftError('此草稿已达200次编辑上限，请保留草稿等待后续发布能力',413)
    raw=text.encode('utf-16-le');start=data.get('start');end=data.get('end')
    before=span(raw,start,end);after=data.get('replacement')
    if before!=data.get('quote'):raise DraftError('选中文字与草稿不一致，请重新选择')
    if not isinstance(after,str):raise DraftError('替换文字必须为文本',400)
    try:after_raw=after.encode('utf-16-le')
    except UnicodeError as exc:raise DraftError('替换文字包含不完整字符',400) from exc
    if max(units(before),len(after_raw)//2)>MAX_SELECTION:raise DraftError('单次编辑最多4000 UTF-16 单位',413)
    if before==after:raise DraftError('正文未变化',400)
    result=(raw[:start*2]+after_raw+raw[end*2:]).decode('utf-16-le')
    if units(result)>MAX_UNITS:raise DraftError('草稿超过长度上限',413)
    result_raw=result.encode('utf-16-le')
    value=copy.deepcopy(value);rows=[];invalid={};delta=units(after)-(end-start)
    for row in value['items']:
        off=row['offset']
        if start<=off<end:
            invalid[row['id']]=row.get('parent_id')
            value['chapter_issues'].append(dict(item=row,reason='章节标题位于被替换或删除的选区，需重新定位'))
            continue
        if off>=end:row['offset']+=delta
        row['quote']=nav.quote_at(result_raw,row['offset'])
        rows.append(row)
    for row in rows:
        while row.get('parent_id') in invalid:row['parent_id']=invalid[row['parent_id']]
    nav.validate(rows,result)
    value['items']=rows
    value['operations'].append(dict(id=ident,start=start,end=end,before=before,after=after,request_digest=fingerprint,author_id='user',created=stamp()))
    value.update(revision=value['revision']+1,updated=stamp(),updated_by='user',cursor_offset=start)
    return value,result,False


def change_chapter(rows,text,data):
    """Reuse full navigation validation; explicit deletion promotes children."""
    rows=copy.deepcopy(rows);ident=data.get('id');action=data.get('action','upsert')
    target=next((row for row in rows if row['id']==ident),None)
    if action=='delete':
        if not target:raise DraftError('目录项已变化，请刷新目录')
        rows.remove(target)
        for row in rows:
            if row.get('parent_id')==ident:row['parent_id']=target.get('parent_id')
    elif action=='upsert':
        if ident and not target:raise DraftError('目录项已变化，请刷新目录')
        start=data.get('start');end=data.get('end');raw=text.encode('utf-16-le')
        selected=span(raw,start,end)
        if not selected or selected!=data.get('quote'):raise DraftError('章节选区与正文不一致，请重新选择')
        title=data.get('title')
        if not isinstance(title,str):raise DraftError('请输入章节标题',400)
        row=dict(id=ident or 'manual_'+secrets.token_hex(10),title=title,offset=start,parent_id=data.get('parent_id'),quote=nav.quote_at(raw,start),basis='quick_selection')
        if target:rows[rows.index(target)]=row
        else:rows.append(row)
    else:raise DraftError('目录操作无效',400)
    byid={r['id']:r for r in rows};out=[];visiting=set();children={}
    for row in rows:children.setdefault(row.get('parent_id'),[]).append(row)
    for group in children.values():group.sort(key=lambda r:r['offset'])
    def visit(row,depth=0):
        if row['id'] in visiting or depth>=nav.MAX_DEPTH:raise DraftError('目录层级循环或超过8层',400)
        visiting.add(row['id']);out.append(row)
        for child in children.get(row['id'],[]):visit(child,depth+1)
    for row in rows:
        parent=row.get('parent_id')
        if parent and (parent not in byid or byid[parent]['offset']>row['offset']):raise DraftError('上级章节须存在且位于本章之前',400)
    for row in children.get(None,[]):visit(row)
    if len(out)!=len(rows):raise DraftError('目录层级存在循环',400)
    checked=nav.validate(out,text)
    for row,original in zip(checked,out):row['basis']=original.get('basis','administrator_verified')
    return checked


def save(root,name,value):
    raw=(json.dumps(value,ensure_ascii=False,separators=(',',':'))+'\n').encode()
    if len(raw)>MAX_FILE:raise DraftError('草稿元数据超过4MB上限，请保留当前草稿',413)
    target=path_for(root,name);target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.draft-',dir=target.parent)
    try:
        with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
        os.chmod(tmp,0o600);os.replace(tmp,target)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def navigation_guard(root,name):
    from member_store import locked
    target=nav.metadata_path(root,name)
    target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    return locked(str(target))
