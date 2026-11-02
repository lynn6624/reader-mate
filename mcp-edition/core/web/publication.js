/* Publish the shared administrator draft and inspect bounded recovery history. */
const publicationState={name:'',busy:false,generation:0,preview:null,attempt:null,retryable:false,history:[]};
const publicationStyle=document.createElement('style');
publicationStyle.textContent=`
#btnPublication[hidden],#publicationDialog[hidden],#publicationDialog [hidden]{display:none}
#publicationDialog .rm-review-body{width:min(800px,100%)}
.rm-publication-summary{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:6px 16px}
.rm-publication-summary dt,.rm-publication-summary dd{margin:0;overflow-wrap:anywhere}
.rm-publication-check{display:flex;gap:8px;align-items:flex-start;margin-top:12px}
.rm-publication-check input{flex:none;margin-top:5px}
#publicationDialog label,#publicationDialog button{min-height:44px}
#publicationDialog .rm-publication-history{overflow-wrap:anywhere}
#publicationDialog .rm-publication-issue{margin:8px 0;overflow-wrap:anywhere}
`;
document.head.append(publicationStyle);
const publicationEntry=document.createElement('button');
publicationEntry.id='btnPublication';publicationEntry.className='btn';publicationEntry.type='button';
publicationEntry.textContent='发布与回滚';publicationEntry.hidden=true;
publicationEntry.style.cssText='min-height:44px;margin-top:8px';
$('#btnReview').after(publicationEntry);
const publicationDialog=document.createElement('div');
publicationDialog.id='publicationDialog';publicationDialog.className='rm-review';publicationDialog.hidden=true;
publicationDialog.innerHTML=`<section class="rm-review-body" role="dialog" aria-modal="true" aria-labelledby="publicationTitle">
<div class="rm-review-head"><strong id="publicationTitle">发布与回滚</strong><button id="publicationClose" class="btn" type="button">关闭</button></div>
<p id="publicationBook"></p>
<p>发布会将本书整份共享草稿切换为正式正文，并迁移目录、批注和阅读进度；不会修改身份。定位异常请回到草稿调整，再重新校验。</p>
<div class="rm-review-head"><button id="publicationPreview" class="btn" type="button">校验共享草稿</button><button id="publicationHistoryRefresh" class="btn" type="button">刷新恢复记录</button></div>
<p id="publicationMessage" role="status" aria-live="polite"></p>
<article id="publicationPreviewCard" class="rm-review-card" hidden>
<h3 id="publicationPreviewTitle"></h3><div id="publicationSummary"></div><div id="publicationIssues"></div>
<label id="publicationAcknowledgeLabel" class="rm-publication-check"><input id="publicationAcknowledge" type="checkbox"><span id="publicationAcknowledgeText"></span></label>
<button id="publicationConfirm" class="btn primary" type="button" disabled>确认发布</button>
<button id="publicationRetry" class="btn" type="button" hidden>重试本次确认</button>
</article>
<h3>短期恢复记录</h3><p>仅可选择允许清理的旧恢复材料。清理不会改动当前正文，但选中材料清理后无法再用于回滚。</p>
<div id="publicationHistory" class="rm-publication-history"></div>
<button id="publicationCleanupPreview" class="btn" type="button" disabled>预览清理所选旧记录</button>
</section>`;
document.body.append(publicationDialog);

function publicationNode(tag,text,cls=''){
  const node=document.createElement(tag);node.textContent=text;if(cls)node.className=cls;return node;
}
function publicationButton(text,fn,available=true){
  const button=publicationNode('button',text,'btn');button.type='button';button.dataset.publicationEnabled=available?'1':'0';button.onclick=fn;return button;
}
function publicationCount(value){return Number.isSafeInteger(value)&&value>=0?value.toLocaleString():'—';}
function publicationBytes(value){
  if(!Number.isFinite(value)||value<0)return '未知大小';
  return value>=1024*1024?(value/(1024*1024)).toFixed(2)+' MiB':Math.ceil(value/1024)+' KiB';
}
function publicationRequestId(){
  const suffix=typeof crypto.randomUUID==='function'?crypto.randomUUID():Date.now().toString(36)+'_'+Math.random().toString(36).slice(2);
  return 'publication_'+suffix;
}
function publicationSelectedIds(){
  return [...$('#publicationHistory').querySelectorAll('input[data-publication-id]:checked')].map(node=>node.dataset.publicationId);
}
function publicationSync(){
  const busy=publicationState.busy,preview=publicationState.preview;
  publicationDialog.setAttribute('aria-busy',String(busy));
  for(const node of publicationDialog.querySelectorAll('button,input'))node.disabled=busy||node.dataset.publicationEnabled==='0';
  $('#publicationConfirm').disabled=busy||!preview?.valid||!preview.canCommit||!$('#publicationAcknowledge').checked;
  $('#publicationAcknowledge').disabled=busy||!preview?.valid||!preview.canCommit;
  $('#publicationRetry').hidden=!publicationState.retryable||!publicationState.attempt;
  $('#publicationRetry').disabled=busy||!publicationState.retryable||!publicationState.attempt;
  $('#publicationCleanupPreview').disabled=busy||!publicationSelectedIds().length;
  publicationEntry.disabled=busy;
  // Disabling the focused control during a request moves focus to the document.
  // Put it back inside the modal when requests finish so Escape/Tab stay local.
  if(!busy&&!publicationDialog.hidden&&!publicationDialog.contains(document.activeElement))$('#publicationClose').focus();
}
function publicationInvalidate(message=''){
  if(publicationState.preview)publicationState.preview.valid=false;
  $('#publicationAcknowledge').checked=false;
  if(message)$('#publicationMessage').textContent=message;
  publicationSync();
}
function refreshPublicationControls(){
  publicationEntry.hidden=!canManageLibrary||!state.book;
  if(!publicationDialog.hidden&&(!canManageLibrary||!state.book||state.book.name!==publicationState.name)){
    publicationInvalidate('书籍或权限已变化，请重新打开后校验。');
    if(!publicationState.busy)publicationClose();
  }
}
async function publicationApi(path,data){
  let response;
  try{
    response=await fetchAuth(path,data===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
  }catch(cause){const error=Error('连接中断，请检查网络后重试。');error.retryable=true;throw error;}
  let result;
  try{result=await response.json();}
  catch(cause){const error=Error('未收到完整结果，请重新校验当前状态。');error.status=response.status;error.retryable=response.ok||response.status>=500;throw error;}
  if(!response.ok||!result||result.ok!==true){
    const error=Error(typeof result?.error==='string'?result.error:'发布请求失败');
    error.status=response.status;error.retryable=response.status>=500;throw error;
  }
  return result;
}
function publicationClose(){
  if(publicationState.busy)return;
  publicationDialog.hidden=true;publicationState.generation++;navigationOpen=false;
  $('#btnLib').focus();
}
$('#publicationClose').onclick=publicationClose;
publicationDialog.addEventListener('keydown',event=>{
  event.stopPropagation();
  if(event.key==='Escape'){publicationClose();return;}
  if(event.key==='Tab'){
    const nodes=[...publicationDialog.querySelectorAll('button,input,[tabindex]')].filter(node=>!node.disabled&&node.getClientRects().length);
    const first=nodes[0],last=nodes[nodes.length-1];
    if(first&&event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}
    else if(last&&!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}
  }
});
publicationEntry.onclick=async()=>{
  if(publicationState.busy||!canManageLibrary||!state.book)return;
  if(draftDirty()){toast('请先保存当前草稿输入，再打开发布与回滚。',5000);return;}
  if(!await closeDraftEditor())return;
  closePanels();navigationOpen=true;clearTimeout(saveTimer);saveTimer=null;
  // Stop speech only after suppressing its normal final progress save.
  if(tts.state!=='idle')ttsStopInternal(true);
  if(publicationState.name!==state.book.name){
    publicationState.name=state.book.name;publicationState.preview=null;publicationState.attempt=null;publicationState.retryable=false;
    $('#publicationPreviewCard').hidden=true;
  }
  $('#publicationBook').textContent='书籍：'+publicationState.name;
  publicationDialog.hidden=false;$('#publicationClose').focus();
  await publicationLoadHistory();
};
$('#publicationPreview').onclick=()=>publicationPreview('publish');
$('#publicationHistoryRefresh').onclick=()=>publicationLoadHistory();
$('#publicationAcknowledge').onchange=publicationSync;
$('#publicationConfirm').onclick=()=>publicationCommit(false);
$('#publicationRetry').onclick=()=>publicationCommit(true);
$('#publicationCleanupPreview').onclick=()=>publicationPreview('cleanup');

async function publicationLoadHistory(){
  if(publicationState.busy)return;
  const generation=++publicationState.generation;publicationState.busy=true;publicationSync();
  try{
    const result=await publicationApi('/api/publication/history?'+new URLSearchParams({name:publicationState.name}));
    if(generation!==publicationState.generation)return;
    if(!Array.isArray(result.items))throw Error('恢复记录格式无效，请刷新重试。');
    const selected=new Set(publicationSelectedIds());publicationState.history=result.items;
    $('#publicationHistory').replaceChildren();
    if(!result.items.length)$('#publicationHistory').append(publicationNode('p','暂无恢复记录。'));
    for(const row of result.items){
      const card=publicationNode('article','','rm-review-card');card.dataset.publicationId=String(row.publication_id);
      const action=row.action==='rollback'?'回滚':'发布';
      card.append(publicationNode('h4',action+' · '+(row.created_at||'时间未知')),
        publicationNode('p','记录：'+row.publication_id+' · 状态：'+row.status+' · '+publicationBytes(row.bytes),'rm-review-meta'));
      if(row.can_rollback===true)card.append(publicationButton('校验回滚影响',()=>publicationPreview('rollback',row.publication_id)));
      if(row.can_cleanup===true){
        const label=publicationNode('label','','rm-publication-check'),input=document.createElement('input');
        input.type='checkbox';input.dataset.publicationId=String(row.publication_id);input.checked=selected.has(String(row.publication_id));
        input.onchange=()=>{
          if(publicationState.preview?.action==='cleanup')publicationInvalidate('清理选择已变化，请重新预览。');
          publicationSync();
        };
        label.append(input,publicationNode('span','选择这份旧恢复材料进行清理'));card.append(label);
      }
      $('#publicationHistory').append(card);
    }
    if(!publicationState.preview)$('#publicationMessage').textContent='请先校验共享草稿，或选择一条恢复记录校验回滚影响。';
  }catch(error){publicationInvalidate(error.message+' 已有摘要仍保留，请重新校验后确认。');}
  finally{publicationState.busy=false;publicationSync();}
}
function publicationRenderPreview(preview){
  const result=preview.result;$('#publicationPreviewCard').hidden=false;
  $('#publicationSummary').replaceChildren();$('#publicationIssues').replaceChildren();
  $('#publicationAcknowledge').checked=false;
  if(preview.action==='cleanup'){
    $('#publicationPreviewTitle').textContent='清理影响';
    $('#publicationSummary').append(publicationNode('p','将清理 '+publicationCount(result.count)+' 份旧恢复材料，释放约 '+publicationBytes(result.bytes)+'。当前正文、批注和阅读进度保留。'));
    $('#publicationAcknowledgeText').textContent='我确认清理以上旧恢复材料，清理后无法再用它们回滚。';
    $('#publicationConfirm').textContent='确认清理旧恢复材料';
  }else{
    const rollback=preview.action==='rollback',summary=result.summary||{};
    $('#publicationPreviewTitle').textContent=rollback?'回滚影响':'整份共享草稿发布影响';
    const list=document.createElement('dl');list.className='rm-publication-summary';
    const rows=[['正文长度',publicationCount(summary.old_units)+' → '+publicationCount(summary.new_units)],
      ['草稿编辑',publicationCount(summary.edit_count)],['划线',publicationCount(summary.highlight_count)],
      ['批注',publicationCount(summary.annotation_count)],['阅读进度',publicationCount(summary.progress_count)],['目录项',publicationCount(summary.chapter_count)]];
    for(const [label,count] of rows)list.append(publicationNode('dt',label),publicationNode('dd',count));
    $('#publicationSummary').append(list);
    const issues=Array.isArray(result.issues)?result.issues:[],total=Number.isSafeInteger(result.issues_total)?result.issues_total:issues.length;
    $('#publicationIssues').append(publicationNode('p','定位异常：'+publicationCount(total)+(total?'。请先核对处理，再重新校验。':'。')));
    for(const issue of issues.slice(0,100))$('#publicationIssues').append(publicationNode('p',
      [issue.record_id,issue.message||issue.code].filter(Boolean).join('：'),'rm-review-issue rm-publication-issue'));
    if(total>Math.min(issues.length,100))$('#publicationIssues').append(publicationNode('p','此处仅显示部分异常；请返回草稿逐项核对。'));
    $('#publicationAcknowledgeText').textContent=rollback?'我已核对影响，确认回滚正文并迁移当前批注和阅读进度。':'我已核对影响，确认发布本书整份共享草稿。';
    $('#publicationConfirm').textContent=rollback?'确认回滚':'确认发布整份草稿';
  }
  $('#publicationAcknowledgeLabel').hidden=!preview.canCommit;
  $('#publicationMessage').textContent=preview.canCommit?'校验完成。请核对摘要，再勾选确认。':'当前不能提交。已有内容和记录保留，请处理异常或重新校验。';
  publicationSync();
}
async function publicationPreview(action,publicationId=''){
  if(publicationState.busy||!canManageLibrary||state.book?.name!==publicationState.name)return;
  const ids=action==='cleanup'?publicationSelectedIds():[];
  if(action==='cleanup'&&!ids.length)return;
  publicationState.busy=true;publicationInvalidate('正在校验影响…');
  try{
    const payload={name:publicationState.name};
    if(action==='rollback')payload.publication_id=publicationId;
    if(action==='cleanup')payload.publication_ids=ids;
    const result=await publicationApi('/api/publication/'+(action==='publish'?'preview':action==='rollback'?'rollback-preview':'cleanup-preview'),payload);
    const digest=action==='cleanup'?result.digest:result.preview_digest;
    if(typeof digest!=='string'||!digest||(action!=='cleanup'&&(result.action!==action||typeof result.can_commit!=='boolean'))){
      throw Error('校验结果格式无效，请重新校验。');
    }
    publicationState.preview={action,publicationId,ids,digest,result,valid:true,canCommit:action==='cleanup'?result.count>0:result.can_commit};
    publicationState.attempt=null;publicationState.retryable=false;
    publicationRenderPreview(publicationState.preview);
  }catch(error){publicationInvalidate(error.message+' 已有摘要仍保留，请重新校验。');}
  finally{publicationState.busy=false;publicationSync();}
}
async function publicationCommit(retry){
  if(publicationState.busy||!canManageLibrary||state.book?.name!==publicationState.name)return;
  const preview=publicationState.preview;
  if(retry){if(!publicationState.retryable||!publicationState.attempt)return;}
  else{
    if(!preview?.valid||!preview.canCommit||!$('#publicationAcknowledge').checked)return;
    const payload={name:publicationState.name},action=preview.action;
    if(action==='cleanup')Object.assign(payload,{publication_ids:[...preview.ids],confirm_digest:preview.digest});
    else{
      Object.assign(payload,{preview_digest:preview.digest,request_id:publicationRequestId()});
      if(action==='rollback')payload.publication_id=preview.publicationId;
    }
    publicationState.attempt={action,payload,path:'/api/publication/'+(action==='publish'?'commit':action==='rollback'?'rollback':'cleanup')};
  }
  const attempt=publicationState.attempt;publicationState.busy=true;publicationState.retryable=false;
  publicationInvalidate('正在确认，请勿重复操作…');
  let result;
  try{result=await publicationApi(attempt.path,attempt.payload);}
  catch(error){
    publicationState.retryable=error.retryable===true;
    $('#publicationMessage').textContent=error.message+(publicationState.retryable?' 上次确认结果尚未确定；可重试本次确认，或重新校验当前状态。':' 请重新校验影响，再确认。');
  }
  if(!result){publicationState.busy=false;publicationSync();return;}
  publicationState.attempt=null;publicationState.retryable=false;publicationState.preview=null;
  $('#publicationPreviewCard').hidden=true;
  if(attempt.action==='cleanup'){
    publicationState.busy=false;publicationSync();toast('所选旧恢复材料已清理，当前正文和阅读记录保留。',5000);
    await publicationLoadHistory();return;
  }
  const name=publicationState.name,label=attempt.action==='rollback'?'回滚':'发布';
  publicationDialog.hidden=true;publicationState.generation++;closePanels();navigationOpen=true;
  try{
    if(typeof publicationRefreshBook!=='function')throw Error('正式阅读界面尚未就绪');
    await publicationRefreshBook(name);
    toast(label+'已完成；当前为临时阅读位置，请按需设置续读点。',6000);
  }catch(error){toast(label+'已完成，但正文刷新失败：'+error.message+'。请刷新页面后核对正式版本。',9000);}
  finally{navigationOpen=false;publicationState.busy=false;publicationSync();refreshPublicationControls();}
}
refreshPublicationControls();
