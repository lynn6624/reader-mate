/* Administrator draft and quick chapters; inserted into the existing reader script. */
let draftEditor=null, draftEditTimer=null, chapterEditor=null;
function refreshDraftControls(){
  const active=!!state.book?.isDraft;
  $('#btnDraft').hidden=!canManageLibrary || !state.book;
  $('#btnDraft').textContent=active?'草稿中':'草稿';
  $('#draftBanner').hidden=!active;document.body.classList.toggle('draft-mode',active);
  if(active)$('#draftBanner').firstChild.textContent='草稿预览 · 已保存第 '+state.book.draft_revision+' 版 · '+state.book.edit_count+' 次文字编辑 · '+(state.book.chapter_issues||[]).length+' 项章节待核对 ';
  if(active)document.body.style.setProperty('--draft-banner-h',$('#draftBanner').offsetHeight+'px');
  $('#btnKeepPosition').hidden=active || !state.temporaryView;
  $('#btnSearch').disabled=active;
  for(const id of ['btnTts','btnTtsPlay','btnTtsResume'])$('#'+id).disabled=active;
}
async function editorRequest(path,data){
  const r=await fetchAuth(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
  const j=await r.json();if(!r.ok || !j.ok){const e=Error(j.error||'保存失败');e.status=r.status;throw e;}return j;
}
function applyDraftView(data){
  Object.assign(state.book,data,{chars:data.total_units,progress:{}});
  state.preciseOffset=null;installReadingWindow(data,data.cursor_offset||0);setTemporaryView(true);refreshDraftControls();
}
async function enterDraft(selection=null){
  if(!canManageLibrary || !state.book)throw Error('请先以管理员打开一本书');
  if(state.book.isDraft)return true;
  if(tts.state!=='idle')ttsStopInternal(true);
  clearTimeout(saveTimer);saveTimer=null;
  const book=state.book;
  const data=await editorRequest('/api/draft/start',{name:book.name,book_revision:book.book_revision,view_offset:selection?.start||0});
  if(state.book!==book)throw Error('书目已切换，请重新进入草稿');
  closePanels();closeHlPanel();notesCache.list=[];notesCache.name='';notesCache.revision='';
  state.book={...data,chars:data.total_units,progress:{}};applyDraftView(data);
  if(selection && data.edit_count){toast('已打开现有草稿，请在草稿中重新选择文字',5000);return false;}
  return true;
}
$('#btnDraft').onclick=()=>enterDraft().catch(e=>toast(e.message,6000));
$('#exitDraft').onclick=async()=>{if(await closeDraftEditor())await openBook(state.book.name);};
async function openDraftEditor(info){
  if(!canManageLibrary || !info)return;
  const chosen={...info};
  if(chosen.text.length>4000)throw Error('单次最多选4000 UTF-16单位');
  if(!await enterDraft(chosen))return;
  if(bookSlice(chosen.start,chosen.end)!==chosen.text)throw Error('草稿位置已变化，请重新选择文字');
  window.getSelection().removeAllRanges();
  draftEditor={book:state.book,start:chosen.start,end:chosen.end,quote:chosen.text,saved:chosen.text,inflight:null,pending:null,blocked:false};
  $('#draftReplacement').value=chosen.text;$('#draftEditStatus').textContent='修改后自动保存；正式正文不变。';
  $('#draftEditDialog').hidden=false;$('#draftReplacement').focus();
}
function draftDirty(){return !!draftEditor && ($('#draftReplacement').value!==draftEditor.saved || !!draftEditor.pending || !!draftEditor.inflight);}
async function flushDraftEdit(){
  clearTimeout(draftEditTimer);draftEditTimer=null;
  const edit=draftEditor;if(!edit)return true;
  if(edit.inflight)await edit.inflight;
  if(edit!==draftEditor || state.book!==edit.book)return false;
  if(edit.blocked)return false;
  const replacement=$('#draftReplacement').value;
  if(replacement===edit.saved && !edit.pending)return true;
  const request=edit.pending || {name:edit.book.name,base_revision:edit.book.book_revision,expected_revision:edit.book.draft_revision,
    request_id:'edit_'+Date.now().toString(36)+'_'+Math.random().toString(36).slice(2),start:edit.start,end:edit.end,quote:edit.quote,replacement};
  edit.pending=request;$('#draftEditStatus').textContent='正在保存…';
  const operation=(async()=>{
    try{
      const data=await editorRequest('/api/draft/edit',request);
      if(draftEditor!==edit || state.book!==edit.book)return false;
      if(data.draft_revision!==request.expected_revision+1){edit.blocked=true;throw Error('其他编辑已更新草稿，请先复制当前输入，完成后重新进入草稿核对');}
      edit.saved=request.replacement;edit.quote=request.replacement;edit.end=edit.start+request.replacement.length;edit.pending=null;
      applyDraftView(data);$('#draftEditStatus').textContent='已保存到草稿 · 第 '+data.draft_revision+' 版';return true;
    }catch(e){
      if(e.status===409)edit.blocked=true;
      $('#draftEditStatus').textContent=e.message+'；当前输入仍保留，可复制。';return false;
    }
  })();
  edit.inflight=operation;const ok=await operation;if(edit.inflight===operation)edit.inflight=null;
  if(ok && $('#draftReplacement').value!==edit.saved)return flushDraftEdit();return ok;
}
$('#draftReplacement').oninput=()=>{
  clearTimeout(draftEditTimer);$('#draftEditStatus').textContent='尚未保存…';
  draftEditTimer=setTimeout(flushDraftEdit,800);
};
$('#draftDelete').onclick=()=>{ $('#draftReplacement').value='';flushDraftEdit(); };
$('#draftRetry').onclick=()=>flushDraftEdit();
async function closeDraftEditor(){
  if(!draftEditor)return true;
  if(draftDirty() && !await flushDraftEdit()){
    if(!confirm('此处输入尚未保存。先复制需要保留的文字，确定放弃未保存输入并关闭？'))return false;
  }
  clearTimeout(draftEditTimer);draftEditor=null;$('#draftEditDialog').hidden=true;return true;
}
$('#draftEditClose').onclick=()=>closeDraftEditor();
window.addEventListener('beforeunload',e=>{if(draftDirty()){e.preventDefault();e.returnValue='';}});
document.addEventListener('visibilitychange',()=>{if(document.hidden && draftDirty())flushDraftEdit();});

async function chapterTable(book){
  let offset=0,table=null,rows=[];
  do{
    const r=await fetchAuth((book.isDraft?'/api/draft/navigation?':'/api/navigation?')+new URLSearchParams({name:book.name,offset:String(offset),limit:'500'}));
    const j=await r.json();if(!r.ok || !j.ok)throw Error(j.error||'读取目录失败');
    if(j.book_revision!==book.book_revision || (table && table.revision!==j.revision))throw Error('目录已变化，请重试');
    if(j.status==='invalid_metadata' || j.status==='content_changed')throw Error('请先核对目录元数据');
    table=j;rows.push(...j.items);offset=j.next_offset;
  }while(offset!==null);
  if(state.book!==book)throw Error('书目已切换');return {...table,items:rows};
}
async function openChapterEditor(selection,item=null){
  if(!canManageLibrary || !state.book)return;
  const book=state.book,table=await chapterTable(book);
  const info=selection || {start:item.offset,end:item.offset+item.quote.length,text:item.quote};
  const existing=item || table.items.find(row=>row.offset===info.start);
  chapterEditor={book,table,info:{...info},id:existing?.id||null};
  $('#chapterTitle').value=existing?.title||info.text.trim().slice(0,160);
  const parent=$('#chapterParent');parent.replaceChildren(new Option('顶层章节',''));
  for(const row of table.items)if(row.id!==existing?.id)parent.add(new Option(row.title,row.id));
  parent.value=existing?.parent_id||'';
  const resolve=$('#chapterResolve');resolve.replaceChildren(new Option('新建目录项',''));
  for(const issue of table.chapter_issues||[])resolve.add(new Option(issue.item.title,issue.item.id));
  // Select and its preceding label are separate nodes; keep the dialog visible.
  resolve.hidden=!book.isDraft || !(table.chapter_issues||[]).length;
  resolve.previousElementSibling.hidden=resolve.hidden;
  $('#chapterStatus').textContent=book.isDraft?'保存到草稿目录。':'保存正式版目录元数据；正文和进度不变。';
  $('#chapterSave').disabled=false;$('#chapterDialog').hidden=false;$('#chapterTitle').focus();
}
$('#chapterClose').onclick=()=>{chapterEditor=null;$('#chapterDialog').hidden=true;};
$('#chapterSave').onclick=async()=>{
  const edit=chapterEditor;if(!edit || state.book!==edit.book)return;
  $('#chapterSave').disabled=true;
  const data={name:edit.book.name,book_revision:edit.book.book_revision,base_revision:edit.book.book_revision,
    expected_revision:edit.table.revision,action:'upsert',id:edit.id,start:edit.info.start,end:edit.info.end,quote:edit.info.text,
    title:$('#chapterTitle').value,parent_id:$('#chapterParent').value||null,resolve_issue_id:$('#chapterResolve').value||null};
  try{
    const j=await editorRequest(edit.book.isDraft?'/api/draft/chapter':'/api/chapter',data);
    if(state.book!==edit.book)return;
    if(edit.book.isDraft)applyDraftView(j);
    chapterEditor=null;$('#chapterDialog').hidden=true;window.getSelection().removeAllRanges();
    if(navigationOpen)await tocPage(true);toast('章节已保存');
  }catch(e){$('#chapterStatus').textContent=e.message;}finally{$('#chapterSave').disabled=false;}
};
async function deleteChapter(item){
  if(!canManageLibrary || !state.book || !confirm('删除目录项「'+item.title+'」？子章节会提升一级，正文保留。'))return;
  const book=state.book;
  const data={name:book.name,book_revision:book.book_revision,base_revision:book.book_revision,expected_revision:tocState.revision,action:'delete',id:item.id};
  const j=await editorRequest(book.isDraft?'/api/draft/chapter':'/api/chapter',data);
  if(state.book!==book)return;if(book.isDraft)applyDraftView(j);await tocPage(true);
}

window.addEventListener("resize",()=>{if(state.book?.isDraft)refreshDraftControls();});
