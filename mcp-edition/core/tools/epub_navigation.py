"""Strict, bounded EPUB -> one stable TXT plus native navigation. Standard library only."""
import argparse
import hashlib
import json
import posixpath
import re
import sys
import time
from html.entities import html5
from pathlib import Path
from urllib.parse import unquote,urlsplit
import xml.etree.ElementTree as ET
import zipfile

MAX_ARCHIVE=12*1024*1024
MAX_EXPANDED=32*1024*1024
MAX_MEMBER=4*1024*1024
MAX_OUTPUT=8*1024*1024
MAX_ITEMS=8000
BLOCK={'p','div','section','article','blockquote','pre','li','ul','ol','tr','h1','h2','h3','h4','h5','h6'}
SKIP={'head','script','style','nav','noscript'}


def tag(node):return node.tag.rsplit('}',1)[-1].lower() if isinstance(node.tag,str) else ''
def content(node):return re.sub(r'\s+',' ',''.join(node.itertext())).strip() if node is not None else ''


def reference(base,href):
    url=urlsplit(href)
    if url.scheme or url.netloc:raise ValueError('EPUB目录/正文引用外部资源，无法可靠转换')
    path=posixpath.normpath(posixpath.join(posixpath.dirname(base),unquote(url.path))) if url.path else base
    if path.startswith(('../','/')) or path=='..' or '\\' in path:raise ValueError('EPUB资源路径越界')
    return path,unquote(url.fragment)


class Builder:
    def __init__(self):self.parts=[];self.units=0;self.py=0;self.pending=[];self.anchors={};self.headings=[];self.trailing=0
    def emit(self,text):
        text=re.sub(r'[ \t\r\n\f\v]+',' ',text or '')
        if self.trailing or not self.parts:text=text.lstrip(' ')
        if not text:return
        for ident in self.pending:
            if ident in self.anchors:raise ValueError('EPUB正文存在重复片段ID')
            self.anchors[ident]=(self.units,self.py)
        self.pending=[]
        self.parts.append(text);self.py+=len(text);self.units+=len(text.encode('utf-16-le'))//2;self.trailing=0
        if self.py>MAX_OUTPUT:raise ValueError('EPUB转换后的正文过大')
    def separate(self,count=2):
        if self.parts and self.trailing<count:
            value='\n'*(count-self.trailing);self.parts.append(value);self.py+=len(value);self.units+=len(value);self.trailing=count
    def visit(self,node,depth=0):
        if depth>128:raise ValueError('EPUB正文嵌套过深')
        kind=tag(node)
        if kind in SKIP:return
        if kind in BLOCK:self.separate()
        ident=node.get('id') or node.get('{http://www.w3.org/XML/1998/namespace}id')
        if ident:self.pending.append(ident)
        if kind in {'h1','h2','h3','h4','h5','h6'}:
            self.headings.append(dict(title=content(node),offset=self.units,index=self.py,level=int(kind[1])))
        if kind=='br':self.separate(1)
        self.emit(node.text)
        for child in node:
            self.visit(child,depth+1);self.emit(child.tail)
        if kind in BLOCK:self.separate()


def extract_document(source):
    if isinstance(source,(str,Path)) and Path(source).stat().st_size>MAX_ARCHIVE:raise ValueError('EPUB压缩包上限12MB')
    started=time.monotonic()
    with zipfile.ZipFile(source) as archive:
        infos=archive.infolist();names=[i.filename for i in infos]
        if len(infos)>2048 or len(set(names))!=len(names):raise ValueError('EPUB资源过多或存在重复路径')
        if any(i.flag_bits&1 for i in infos):raise ValueError('不支持加密EPUB资源')
        if any(i.file_size>MAX_MEMBER for i in infos) or sum(i.file_size for i in infos)>MAX_EXPANDED:raise ValueError('EPUB解压内容超过资源限制：单文件4MB/总计32MB')
        def read_xml(path):
            if time.monotonic()-started>20:raise ValueError('EPUB转换超时')
            try:raw=archive.read(path)
            except KeyError as exc:raise ValueError('EPUB引用的资源缺失，未导入任何正文') from exc
            if re.search(br'<!\s*ENTITY\b',raw,re.I):raise ValueError('EPUB包含不支持的XML实体声明')
            def entity(match):
                value=html5.get(match.group(1).decode('ascii')+';')
                return ''.join('&#%d;'%ord(c) for c in value).encode('ascii') if value else match.group()
            raw=re.sub(br'&([A-Za-z][A-Za-z0-9]+);',entity,raw)
            try:return ET.fromstring(raw)
            except ET.ParseError as exc:raise ValueError('EPUB正文或导航XML损坏，未跳过章节') from exc
        container=read_xml('META-INF/container.xml')
        rootfile=next((n for n in container.iter() if tag(n)=='rootfile'),None)
        if rootfile is None or not rootfile.get('full-path'):raise ValueError('EPUB缺少OPF路径')
        opf_path,_=reference('',rootfile.get('full-path'));opf=read_xml(opf_path)
        title=next((content(n) for n in opf.iter() if tag(n)=='title'),'')
        manifest={}
        for node in opf.iter():
            if tag(node)=='item':
                ident=node.get('id');href=node.get('href')
                if not ident or not href or ident in manifest:raise ValueError('EPUB manifest无效')
                path,fragment=reference(opf_path,href)
                if fragment:raise ValueError('EPUB manifest不能带片段')
                manifest[ident]=dict(path=path,media=node.get('media-type',''),properties=node.get('properties','').split())
        spine=next((n for n in opf.iter() if tag(n)=='spine'),None)
        if spine is None:raise ValueError('EPUB缺少阅读顺序spine')
        ordered=[]
        for node in spine:
            if tag(node)!='itemref':continue
            item=manifest.get(node.get('idref'))
            if item is None:raise ValueError('EPUB spine引用未定义正文，拒绝部分导入')
            ordered.append(item)
        if not ordered:raise ValueError('EPUB阅读顺序为空')
        documents={};texts=[];global_units=global_py=0;warnings=[];body_headings=[];image_only=set()
        if title:
            texts.append(title+'\n\n');global_units=len(texts[0].encode('utf-16-le'))//2;global_py=len(texts[0])
        for item in ordered:
            path=item['path']
            if path in documents:raise ValueError('EPUB spine重复同一资源，无法唯一映射')
            if 'nav' in item['properties']:continue
            if item['media'].startswith('image/'):
                if path not in names:raise ValueError('EPUB封面资源缺失')
                image_only.add(path)
                warnings.append('已忽略不含文字的图片资源');continue
            if item['media'] not in ('application/xhtml+xml','text/html') and not (not item['media'] and path.lower().endswith(('.xhtml','.html','.htm'))):raise ValueError('EPUB spine含不支持的正文类型')
            document=read_xml(path);builder=Builder()
            body=next((n for n in document.iter() if tag(n)=='body'),None)
            if body is None:raise ValueError('EPUB正文缺少body')
            builder.visit(body);text=''.join(builder.parts).rstrip()
            if not text:
                if any(tag(n) in ('img','svg','image') for n in body.iter()):
                    image_only.add(path)
                    warnings.append('已忽略不含文字的图片页面');continue
                raise ValueError('EPUB spine有空白正文，拒绝伪成功导入')
            documents[path]=dict(base=global_units,py=global_py,anchors=builder.anchors,length=len(text.encode('utf-16-le'))//2,
                                 first_heading=builder.headings[0] if builder.headings else None)
            for h in builder.headings:
                if h['title'] and h['offset']<documents[path]['length']:
                    body_headings.append(dict(h,offset=h['offset']+global_units,index=h['index']+global_py))
            texts.append(text+'\n\n');global_units+=len(texts[-1].encode('utf-16-le'))//2;global_py+=len(texts[-1])
            if global_units*2>MAX_OUTPUT*2:raise ValueError('EPUB转换后的正文超过限制')
        if not documents:raise ValueError('EPUB没有可提取的正文；不支持纯扫描图书')
        final=''.join(texts).rstrip()+'\n'
        if len(final.encode('utf-8'))>MAX_OUTPUT:raise ValueError('EPUB转换后的UTF-8正文上限8MB')
        nav_items=[]
        def add(label,href,parent,base):
            if not label or len(label)>160:raise ValueError('EPUB目录标题为空或过长')
            index=len(nav_items)
            if index>=MAX_ITEMS:raise ValueError('EPUB目录超过8000项')
            entry=dict(id='e_'+str(index),title=label,parent_id=parent,order=index,basis='epub_native',target=None)
            nav_items.append(entry)
            if href is not None:entry['target']=reference(base,href)
            return entry['id']
        def walk_html_li(li,parent,base,depth=0):
            if depth>=8:raise ValueError('EPUB目录层级超过8层')
            labelnode=next((n for n in li if tag(n) in ('a','span')),None)
            ident=parent
            if labelnode is not None:ident=add(content(labelnode),labelnode.get('href') if tag(labelnode)=='a' else None,parent,base)
            for child in li:
                if tag(child) in ('ol','ul'):
                    for nested in child:
                        if tag(nested)=='li':walk_html_li(nested,ident,base,depth+1)
        native=next((item for item in manifest.values() if 'nav' in item['properties']),None)
        if native:
            nav=read_xml(native['path'])
            section=next((n for n in nav.iter() if tag(n)=='nav' and ('toc' in n.get('{http://www.idpf.org/2007/ops}type','').split() or n.get('role')=='doc-toc')),None)
            if section is not None:
                for tree in section:
                    if tag(tree) in ('ol','ul'):
                        for li in tree:
                            if tag(li)=='li':walk_html_li(li,None,native['path'])
        if not nav_items:
            ncx=manifest.get(spine.get('toc')) or next((m for m in manifest.values() if m['media']=='application/x-dtbncx+xml'),None)
            if ncx:
                root=read_xml(ncx['path'])
                def walk_ncx(node,parent,depth=0):
                    if depth>=8:raise ValueError('EPUB目录层级超过8层')
                    label=next((content(n) for n in node if tag(n)=='navlabel'),'')
                    target=next((n.get('src') for n in node if tag(n)=='content'),None)
                    ident=add(label,target,parent,ncx['path'])
                    for child in node:
                        if tag(child)=='navpoint':walk_ncx(child,ident,depth+1)
                navmap=next((n for n in root.iter() if tag(n)=='navmap'),None)
                if navmap is not None:
                    for node in navmap:
                        if tag(node)=='navpoint':walk_ncx(node,None)
        for item in nav_items:
            target=item.pop('target')
            if target is None:continue
            path,fragment=target;document=documents.get(path)
            if document is None and path in image_only and not fragment:
                position=next(i for i,resource in enumerate(ordered) if resource['path']==path)
                following=next((resource['path'] for resource in ordered[position+1:] if resource['path'] in documents),None)
                if following is not None:
                    document=documents[following]
                    warnings.append('目录入口“'+item['title']+'”指向纯图片扉页，已定位到其后的首个文字正文')
            if document is None:raise ValueError('EPUB目录指向未导入的正文资源，拒绝不完整转换')
            if fragment:
                if fragment not in document['anchors']:raise ValueError('EPUB目录片段ID不存在或指向空白，不能可靠定位')
                offset,index=document['anchors'][fragment]
            elif document['first_heading']:
                heading=document['first_heading'];offset,index=heading['offset'],heading['index']
            else:offset=index=0
            if offset>=document['length']:raise ValueError('EPUB目录定位到正文之外')
            item.update(offset=document['base']+offset,index=document['py']+index)
        for item in reversed(nav_items):
            if 'offset' not in item:
                child=next((n for n in nav_items if n['parent_id']==item['id'] and 'offset' in n),None)
                if child is None:raise ValueError('EPUB目录分组没有有效正文目标')
                item.update(offset=child['offset'],index=child['index'])
        if not nav_items:
            if native:warnings.append('原生导航无可用TOC，已按正文章节元素生成')
            if len(body_headings)>MAX_ITEMS:raise ValueError('EPUB正文章节元素超过8000项，未导入')
            stack=[]
            for h in body_headings[:MAX_ITEMS]:
                while stack and stack[-1][0]>=h['level']:stack.pop()
                ident='h_'+str(len(nav_items));parent=stack[-1][1] if stack else None
                nav_items.append(dict(id=ident,title=h['title'],offset=h['offset'],index=h['index'],parent_id=parent,order=len(nav_items),basis='epub_heading'))
                stack.append((h['level'],ident))
        raw=final.encode('utf-16-le')
        for item in nav_items:
            item['quote']=raw[item['offset']*2:(item['offset']+40)*2].decode('utf-16-le',errors='ignore');item.pop('index',None)
        return dict(title=title,text=final,items=nav_items,warnings=warnings,
                    spine_documents=len(documents),book_revision='sha256:'+hashlib.sha256(final.encode('utf-8')).hexdigest())


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--input',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args()
    try:
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_AS,(160*1024*1024,160*1024*1024))
            resource.setrlimit(resource.RLIMIT_CPU,(22,22))
        except ImportError:pass
        result=extract_document(args.input)
        Path(args.output).write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8',newline='\n')
    except (Exception,MemoryError) as exc:
        # No book text, credential, or local path in errors sent to the browser.
        message=str(exc) if isinstance(exc,ValueError) else 'EPUB转换失败或资源不足；未导入部分正文'
        print(message,file=sys.stderr);return 1
    return 0


if __name__=='__main__':sys.exit(main())
