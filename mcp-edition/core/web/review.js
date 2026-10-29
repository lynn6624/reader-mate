/* Human review of suggestions. All edits are committed by the existing PR5 draft. */
const reviewLabels={pending:'待审',accepted:'已接受',rejected:'已拒绝',conflict:'冲突'};
const reviewActions={replace:'替换',delete:'删除',insert_before:'前插入',insert_after:'后插入',add:'新增',update:'调整'};
let reviewName='',reviewOffset=0,reviewGeneration=0,reviewBusy=false;
const reviewStyle=document.createElement('style');
reviewStyle.textContent=`
#btnReview[hidden],.rm-review[hidden]{display:none}.rm-review{position:fixed;inset:0;z-index:500;background:#0007;display:flex;align-items:center;justify-content:center;padding:12px}
.rm-review-body{background:var(--bg,#f5efe3);color:var(--fg,#302b26);width:min(900px,100%);max-height:94vh;overflow:auto;overscroll-behavior:contain;border-radius:12px;padding:18px;box-sizing:border-box}
.rm-review-head{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.rm-review-head strong{flex:1}.rm-review-card{border:1px solid #8886;border-radius:8px;padding:12px;margin-top:12px}
.rm-review-card h3{margin:0 0 8px}.rm-review-card pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:180px;overflow:auto;font:inherit;background:#8881;padding:8px}
.rm-review-card textarea{box-sizing:border-box;width:100%;min-height:100px;font:inherit;padding:8px}.rm-review-card button{margin:8px 8px 0 0;min-height:44px}
.rm-review-meta{font-size:.85em;overflow-wrap:anywhere;opacity:.8}.rm-review-error{color:#a03020;white-space:pre-wrap}.rm-review-nav{display:flex;gap:8px;margin-top:12px}
@media(max-width:600px){.rm-review{padding:4px}.rm-review-body{max-height:98vh;padding:12px}}
`;
document.head.append(reviewStyle);
const reviewDialog=document.createElement('div');reviewDialog.id='reviewDialog';reviewDialog.className='rm-review';reviewDialog.hidden=true;
reviewDialog.innerHTML=`<section class="rm-review-body" role="dialog" aria-modal="true" aria-labelledby="reviewTitle">
<div class="rm-review-head"><strong id="reviewTitle">修订审批</strong><button id="reviewClose" class="btn">关闭</button></div>
<p id="reviewBook"></p><p>接受后写入本书共享草稿，正式正文仍保持原版。</p>
<div class="rm-review-head"><label>状态 <select id="reviewStatus"><option value="pending">待审</option><option value="">全部</option><option value="accepted">已接受</option><option value="rejected">已拒绝</option><option value="conflict">冲突</option></select></label><button id="reviewRefresh" class="btn">刷新</button></div>
<p id="reviewMessage" role="status"></p><div id="reviewList"></div><div id="reviewNav" class="rm-review-nav"></div></section>`;
document.body.append(reviewDialog);

async function reviewApi(path,data){
  const options=data?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}:{};
  const response=await fetchAuth(path,options),result=await response.json();
  if(!response.ok || !result.ok)throw Error(result.error||'修订请求失败');return result;
}
function reviewNode(tag,text,cls=''){
  const node=document.createElement(tag);node.textContent=text;if(cls)node.className=cls;return node;
}
function reviewButton(text,fn){const b=reviewNode('button',text,'btn');b.type='button';b.onclick=fn;return b;}
function reviewText(container,label,text){container.append(reviewNode('b',label),reviewNode('pre',text||'（空）'));}
function reviewAfter(spec,replacement){return spec.operation==='insert_before'?replacement+spec.quote:spec.operation==='insert_after'?spec.quote+replacement:replacement;}
function reviewLock(value){reviewBusy=value;for(const el of reviewDialog.querySelectorAll('button,select'))el.disabled=value;}
function reviewClose(){if(reviewBusy)return;reviewDialog.hidden=true;reviewGeneration++;$('#btnLib').focus();}
$('#reviewClose').onclick=reviewClose;
reviewDialog.addEventListener('keydown',e=>{
  e.stopPropagation();
  if(e.key==='Escape'){reviewClose();return;}
  // Keep focus within the modal without cancelling native scrolling or editing.
  if(e.key==='Tab'){
    const nodes=[...reviewDialog.querySelectorAll('button,select,textarea,[tabindex]')].filter(el=>!el.disabled && el.getClientRects().length);
    const first=nodes[0],last=nodes[nodes.length-1];
    if(first && e.shiftKey && document.activeElement===first){e.preventDefault();last.focus();}
    else if(last && !e.shiftKey && document.activeElement===last){e.preventDefault();first.focus();}
  }
});
$('#reviewStatus').onchange=()=>{reviewOffset=0;loadProposals();};
$('#reviewRefresh').onclick=()=>loadProposals();
$('#btnReview').onclick=async()=>{
  if(!canManageLibrary || !state.book)return;
  if(!await closeDraftEditor())return;
  closePanels();
  reviewName=state.book.name;reviewOffset=0;$('#reviewBook').textContent='书籍：'+reviewName;
  $('#reviewStatus').value='pending';reviewDialog.hidden=false;$('#reviewClose').focus();await loadProposals();
};
async function loadProposals(){
  if(reviewBusy)return;
  const generation=++reviewGeneration;$('#reviewMessage').textContent='正在读取建议…';
  try{
    const result=await reviewApi('/api/proposals?'+new URLSearchParams({name:reviewName,status:$('#reviewStatus').value,offset:String(reviewOffset),limit:'20'}));
    if(generation!==reviewGeneration)return;
    $('#reviewList').replaceChildren();$('#reviewNav').replaceChildren();
    $('#reviewMessage').textContent=result.total?'共 '+result.total+' 条建议':'当前没有此状态的建议';
    for(const row of result.proposals)renderProposal(row);
    if(reviewOffset)$('#reviewNav').append(reviewButton('上一页',()=>{reviewOffset=Math.max(0,reviewOffset-20);loadProposals();}));
    if(result.next_offset!==null)$('#reviewNav').append(reviewButton('下一页',()=>{reviewOffset=result.next_offset;loadProposals();}));
  }catch(e){if(generation===reviewGeneration)$('#reviewMessage').textContent=e.message;}
}
function renderProposal(row){
  const spec=row.spec,card=reviewNode('article','','rm-review-card');card.dataset.proposalId=row.id;
  card.append(reviewNode('h3',(spec.kind==='chapter'?'章节':'正文')+' · '+reviewActions[spec.operation]+' · '+reviewLabels[row.status]));
  card.append(reviewNode('p','提交者：'+progressReaderLabel(spec.author_id)+' · '+new Date(row.created_at).toLocaleString(),'rm-review-meta'));
  reviewText(card,'原文',spec.quote);
  reviewText(card,spec.kind==='chapter'?'建议章名':'修改后文本',spec.kind==='chapter'?spec.title:reviewAfter(spec,spec.replacement));
  if(spec.kind==='chapter')card.append(reviewNode('p','目标章节：'+(spec.target?.title||'新增')+'；上级：'+(spec.parent?.title||'顶层')));
  card.append(reviewNode('p','理由：'+(spec.reason||'未填写')));
  const context=document.createElement('details');context.append(reviewNode('summary','查看短上下文'),reviewNode('pre',spec.context_before+'【'+spec.quote+'】'+spec.context_after));card.append(context);
  if(row.conflict)card.append(reviewNode('p',row.conflict,'rm-review-error'));
  if(row.status==='accepted')reviewText(card,'实际接受内容',spec.kind==='chapter'?row.decision.decision.title:reviewAfter(spec,row.decision.decision.replacement));
  if(row.status==='pending'){
    const detail=document.createElement('div');
    card.append(reviewButton('预览并审批',async()=>{
      if(reviewBusy)return;
      try{
        const result=await reviewApi('/api/proposals/preview',{name:spec.name,id:row.id});
        detail.replaceChildren();
        if(!result.preview){
          card.querySelector('h3').textContent=(spec.kind==='chapter'?'章节':'正文')+' · '+reviewActions[spec.operation]+' · '+reviewLabels[result.proposal.status];
          detail.append(reviewNode('p',result.proposal.conflict||'建议状态已变化，请刷新','rm-review-error'));
          detail.append(reviewButton('拒绝',()=>decideProposal(row,{action:'reject'},detail)));return;
        }
        const preview=result.preview;
        reviewText(detail,spec.operation.startsWith('insert_')?'插入处锚点':'当前草稿原文',spec.operation.startsWith('insert_')?spec.quote:preview.quote);
        const isText=spec.kind==='edit',field=isText?'replacement':'title';
        const input=document.createElement('textarea');input.value=isText?preview.replacement:preview.title;
        input.maxLength=isText?4000:160;input.setAttribute('aria-label',isText?'接受内容（可手动调整）':'接受章名（可手动调整）');
        if(spec.kind==='chapter' && spec.operation==='delete')input.hidden=true;
        detail.append(reviewNode('p',spec.kind==='chapter' && spec.operation==='delete'?'接受后从草稿目录移除此章节，正文保持不变。':'可手动调整下面的内容，再接受到共享草稿。'),input);
        if(isText){const after=reviewNode('pre',reviewAfter(spec,input.value));input.oninput=()=>{after.textContent=reviewAfter(spec,input.value)||'（空）';};detail.append(reviewNode('b','调整后文本'),after);}
        detail.append(reviewButton('接受当前内容',()=>decideProposal(row,{action:'accept',expected_revision:result.expected_revision,[field]:input.value},detail)));
        detail.append(reviewButton('拒绝',()=>decideProposal(row,{action:'reject'},detail)));
      }catch(e){detail.replaceChildren(reviewNode('p',e.message,'rm-review-error'));}
    }),detail);
  }else if(row.status==='conflict')card.append(reviewButton('拒绝',()=>decideProposal(row,{action:'reject'},card)));
  $('#reviewList').append(card);
}
async function decideProposal(row,decision,detail){
  if(reviewBusy)return;
  if(draftDirty()){detail.append(reviewNode('p','请先保存当前草稿输入，再审批','rm-review-error'));return;}
  reviewLock(true);
  try{
    const result=await reviewApi('/api/proposals/review',{name:row.spec.name,id:row.id,...decision});
    toast(result.proposal.status==='accepted'?'已接受到共享草稿':'已拒绝，正文未改变');
    if(result.proposal.status==='accepted' && state.book?.isDraft && state.book.name===row.spec.name){
      const view=await reviewApi('/api/draft?'+new URLSearchParams({name:row.spec.name}));applyDraftView(view);
    }
    reviewLock(false);await loadProposals();
  }catch(e){detail.append(reviewNode('p',e.message+'；可刷新预览后重试。','rm-review-error'));}
  finally{reviewLock(false);}
}
