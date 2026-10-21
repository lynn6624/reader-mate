"""Synthetic regressions plus optional privately downloaded cloud acceptance fixtures."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from bisect import bisect_right

import reader_server as reader
from chapter_index import INDEX_VERSION, build_index, chapter_at, public_index

ROOT = Path(__file__).parent


def units(text):
    return len(text.encode('utf-16-le')) // 2


def synthetic():
    titles = ['译者序','引言 群体的时代','第一篇 起','第一章 同名','第二章 短章','第二篇 起','第一章 同名','后记']
    text = '一本书😀\n\n目录\n\n' + '\n\n'.join(titles) + '\n\n'
    body_start = units(text)
    expected = []
    for title, prose in zip(titles, ['说明。','引言正文。','','甲。','乙。','','丙。','终。']):
        expected.append((units(text),title))
        text += title + '\n' + (prose + '\n' if prose else '') + '\n'
    index = build_index(text)
    assert [(h['start'],h['title']) for h in index['headings']] == expected
    assert len(index['toc_entries']) == len(titles)
    assert index['toc_regions'][0]['end'] == body_start
    assert chapter_at(index,0)['region'] == 'toc'
    assert sum(h['title'] == '第一章 同名' for h in index['headings']) == 2
    assert all(expected[i+1][0]-p[0] < 200 for i,p in enumerate(expected[:-1]))
    assert chapter_at(index,expected[1][0])['title'] == '引言 群体的时代'
    assert build_index('开头。\n正文。\n结尾。')['headings'] == []
    assert len(build_index('第一章 同名\n甲。\n第二章 短章\n乙。\n第一章 同名\n丙。')['headings']) == 3
    assert [h['title'] for h in build_index('Chapter 1\nText.\nChapter 2\nMore.')['headings']] == ['Chapter 1','Chapter 2']
    assert build_index('第1章 Chapter 1\n内容。')['headings'][0]['title'] == '第1章 Chapter 1'
    unmarked = '这里是正文导读。\n' + '\n'.join(titles[:5]) + '\n' + text[text.index('译者序',text.index('后记')+2):]
    assert build_index(unmarked)['toc_regions'], 'No-marker dense TOC was missed'
    return text, expected, index


def integration(text, expected, index):
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        for name in ('reader_server.py','chapter_index.py','progress_catalog.py','navigation.py'):
            shutil.copy(ROOT/name, base/name)
        shutil.copytree(ROOT/'web',base/'web')
        shutil.copytree(ROOT/'tools',base/'tools')
        (base/'data').mkdir(); (base/'书库').mkdir()
        (base/'书库/test.txt').write_bytes(text.encode('utf-8'))
        (base/'书库/old.txt').write_bytes(text.encode('utf-8'))
        (base/'data/identities.json').write_text(json.dumps({'user':'owner','op001':'reader'}))
        offset = expected[1][0] + units(expected[1][1]) + 1
        pos = reader.text_position(text,offset)
        record = {'offset':offset,'percent':offset/units(text),'chars':units(text),
                  'chapter':'第五章 议会','anchor':re.sub(r'\s+',' ',text[pos[3]:pos[3]+40]),
                  'revision':2,'updated':'2026-10-21T12:11:30',
                  'book_revision':'sha256:'+hashlib.sha256(text.encode()).hexdigest(),
                  'paragraph_idx':pos[0],'char_offset':pos[1]}
        legacy = {k:v for k,v in record.items() if k not in ('book_revision','revision')}
        doc = {'version':1,'updated':'2026-10-21T12:11:30','books':{
            'test.txt':{'user':{},'op001':record},'old.txt':legacy}}
        before = json.dumps(doc,ensure_ascii=False).encode()
        progress = base/'data/progress.json'; progress.write_bytes(before)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0)); port = sock.getsockname()[1]
        env = dict(os.environ,READER_CLOUD='1',READER_PORT=str(port))
        process = subprocess.Popen([sys.executable,str(base/'reader_server.py')],env=env,
                                   stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            for _ in range(100):
                try:
                    req = urllib.request.Request(f'http://127.0.0.1:{port}/api/reading-progress?name=test.txt',
                                                 headers={'X-Who-Token':'reader'})
                    with opener.open(req,timeout=2) as response:
                        result = json.load(response)
                    break
                except OSError:
                    time.sleep(.05)
            else:
                raise AssertionError('server did not start')
            assert result['progress']['chapter'] == '引言 群体的时代'
            assert result['revision'] == 2 and progress.read_bytes() == before
        finally:
            process.terminate(); process.wait(timeout=10)
        tool = [sys.executable,str(base/'tools/rebuild_chapters.py'),'--root',str(base),'--all']
        report = base/'report.json'
        subprocess.run(tool+['--report',str(report)],check=True,capture_output=True)
        assert progress.read_bytes() == before
        backup = base/'backup'
        apply = tool+['--apply','--offline','--port',str(port),'--backup-dir',str(backup),'--report',str(report)]
        subprocess.run(apply,check=True,capture_output=True)
        after = json.loads(progress.read_bytes())
        for old,new in ((record,after['books']['test.txt']['op001']),(legacy,after['books']['old.txt'])):
            assert new['chapter'] == '引言 群体的时代' and new['chapter_index_version'] == INDEX_VERSION
            assert {k:v for k,v in new.items() if k not in ('chapter','chapter_index_version','chapter_location_status')} == {
                k:v for k,v in old.items() if k != 'chapter'}
        assert after['updated'] == doc['updated'] and after['books']['test.txt']['user'] == {}
        stable = progress.read_bytes()
        rerun = subprocess.run(apply,check=True,capture_output=True,text=True)
        assert json.loads(rerun.stdout)['already_current'] and progress.read_bytes() == stable
        rollback = [sys.executable,str(base/'tools/rebuild_chapters.py'),'--root',str(base),
                    '--rollback',str(backup),'--offline','--port',str(port)]
        subprocess.run(rollback,check=True,capture_output=True)
        assert progress.read_bytes() == before
        later = json.loads(before); later['books']['test.txt']['op001']['revision'] = 3
        progress.write_text(json.dumps(later),encoding='utf-8')
        unchanged = progress.read_bytes()
        assert subprocess.run(rollback,capture_output=True).returncode != 0
        assert progress.read_bytes() == unchanged
        # Exercise the existing JS segmenter with server-provided boundaries, including emoji.
        fixture = base/'js-case.json'
        fixture.write_text(json.dumps({'text':text,'index':public_index(index),'expected':expected}),encoding='utf-8')
        js = r'''const fs=require('fs'); const html=fs.readFileSync(process.argv[1],'utf8');
const js=html.match(/<script>([\s\S]*?)<\/script>/)[1]; new Function(js);
const a=js.indexOf('const CHAP_RE'), b=js.indexOf('/* ---------------- 进度');
const split=new Function(js.slice(a,b)+';return splitSegments')();
const d=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
for(const s of split(d.text,d.index)){
 if(d.text.slice(s.start,s.end)!==s.text)throw Error('text moved');
 const expected=s.start<d.expected[0][0]?'目录':[...d.expected].reverse().find(x=>x[0]<=s.start)?.[1]||'';
 if(s.chapter!==expected)throw Error('JS/server chapter disagreement');
}'''
        subprocess.run(['node','-e',js,str(base/'web/reader.html'),str(fixture)],check=True,capture_output=True)


# Independently inspected boundaries of the exact cloud edition; no parser-produced oracle.
CROWD_BODY = [(410,'译者序'),(1876,'1902年第六版作者序言'),(4273,'1963年版序言'),(4649,'引言 群体的时代'),
 (9100,'第一篇 群体的灵魂'),(9111,'第一章 群体的普遍特征和群体思维的心理法则'),(14220,'第二章 群体的情感与道德'),
 (26841,'第三章 群体的观念、推理和想象力'),(32208,'第四章 群体信念的宗教形式'),(35367,'第二篇 群体的意见和信仰'),
 (35381,'第一章 群体的信仰和意见的遥远成因'),(46432,'第二章 群体意见的即时成因'),(54339,'第三章 群体的领袖和他们的说服手段'),
 (66986,'第四章 群体的信仰和意见的可变范围'),(73526,'第三篇 群体的归类和描述'),(73540,'第一章 群体的归类'),
 (75437,'第二章 所谓的犯罪群体'),(77852,'第三章 刑事法庭的陪审员'),(81880,'第四章 选民群体'),(87098,'第五章 议会')]


def cloud_fixtures(path):
    base = Path(path)
    text = (base/'crowd.txt').read_text(encoding='utf-8-sig')
    raw = text.encode('utf-16-le'); index = build_index(text)
    assert [(h['start'],h['title']) for h in index['headings']] == CROWD_BODY
    toc_fifth = next(e for e in index['toc_entries'] if e['title'] == '第五章 议会')
    assert toc_fifth['start'] == 388 and chapter_at(index,388)['region'] == 'toc'
    assert len(index['toc_entries']) == 21 and len(index['headings']) == 20
    assert [e['title'] for e in index['toc_entries'] if not e['body_offsets']] == ['作家榜经典文库']
    probes = []
    offsets = {0,388,600,700,750,1500,4500,4520,4522,4648,4649,6100,99000,*range(0,units(text),997)}
    for start,_ in CROWD_BODY:
        offsets.update((start-1,start,start+1))
    for offset in sorted(offsets):
        i = bisect_right([p[0] for p in CROWD_BODY],offset)-1
        expected = '目录' if offset < 410 else CROWD_BODY[i][1]
        assert chapter_at(index,offset)['title'] == expected, offset
        if offset in (0,388,600,700,750,1500,4500,4520,4522,4649,6100,99000):
            probes.append({'offset':offset,**chapter_at(index,offset),
                           'context':raw[offset*2:(offset+40)*2].decode('utf-16-le')})
    prefix = raw[:6100*2].decode('utf-16-le')
    assert prefix.count('\n') == 149 and units(prefix.rsplit('\n',1)[-1]) == 84
    assert raw[6100*2:6120*2].decode('utf-16-le').startswith('工厂和大地的所有权')
    report = {'crowd':index,'probes':probes,'samples':{}}
    for label in ('dreams','no-toc','webnovel'):
        book = (base/(label+'.txt')).read_text(encoding='utf-8-sig')
        current = build_index(book)
        if label == 'dreams':
            assert current['toc_regions'] == [{'start':567,'end':903,'basis':'dense_repeated_sequence+body_transition'}]
            top = [h['start'] for h in current['headings'] if re.match(r'^第[一二三四五六七]章',h['title'])]
            assert top == [1717,4413,19950,27258,44404,116330,254386]
            assert len(current['headings']) == 27 and len(current['toc_entries']) == 30
            assert all(e['title'] == '注 释' for e in current['toc_entries'] if not e['body_offsets'])
        elif label == 'no-toc':
            assert current['headings'] == [] and current['toc_regions'] == []
        else:
            expected = []; offset = 0
            for line in book.splitlines(keepends=True):
                title = line.strip()
                if re.match(r'^(?:第[0-9一二三四五六七八九十百千]+章|Chapter [0-9]+)',title) and len(title) <= 100 and offset != 160124:
                    expected.append(offset+units(line[:len(line)-len(line.lstrip())]))
                offset += units(line)
            assert [h['start'] for h in current['headings']] == expected and not current['toc_regions']
        for h in current['headings']:
            quote = book.encode('utf-16-le')[h['start']*2:h['start']*2+len(h['title'].encode('utf-16-le'))].decode('utf-16-le')
            assert quote == h['title']
        report['samples'][label] = {'toc_entries':len(current['toc_entries']), 'body_starts':len(current['headings']),
                                    'headings':current['headings'], 'toc':current['toc_entries']}
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--fixtures'); parser.add_argument('--report')
    args = parser.parse_args()
    text,expected,index = synthetic(); integration(text,expected,index)
    report = cloud_fixtures(args.fixtures) if args.fixtures else {'synthetic':'passed'}
    if args.report:
        Path(args.report).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print('PASS: dense/no-marker TOC, real body boundaries, short/repeated chapters, UTF-16, legacy rebuild, idempotency/rollback, JS parity' +
          ('; private cloud fixtures and independent boundary oracle' if args.fixtures else ''))
