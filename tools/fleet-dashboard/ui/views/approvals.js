/* Fleet route module: typed inbox. */
(function registerApprovalsView() {
  'use strict';

  let context;
  let selectedKey = '';
  let detailOpen = false;
  const ACTIONABLE_LIMIT = 50;
  const HISTORY_LIMIT = 50;
  const REVIEW_STATUSES = new Set(['submitted', 'reviewing', 'in_review']);

  function useContext(nextContext) { context = nextContext; }

  function loadStyles() {
    if (typeof document === 'undefined') return;
    if (document.querySelector('link[data-fleet-view-style="approvals"]')) return;
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = '/ui/views/approvals.css';
    link.dataset.fleetViewStyle = 'approvals';
    document.head.append(link);
  }

  function sourceState() {
    const {centralLabels, fleetData} = context;
    const labels = Array.isArray(centralLabels) ? centralLabels : [];
    const connected = fleetData && typeof fleetData === 'object' ? fleetData : {};
    if (!labels.some(label => !connected[label]) && (labels.length || Object.keys(connected).length)) return '';
    return `<aside class="approvals-source-state" role="status" data-inbox-state="partial"><strong>Inbox sources are not available yet.</strong><span>Counts cover connected sources only. Existing items remain available while retrying.</span></aside>`;
  }

  function reviewRow({central, board, ticket}) {
    const {esc, fmt, ticketHref} = context;
    return `<article class="approvals-review-row" data-inbox-source="review-conversation"><header class="approvals-review-head"><div class="approvals-route"><a class="id" href="${ticketHref(central, board.board_id, ticket.id)}">${esc(ticket.id)}</a><span>${esc(central)} / ${esc(board.label)}</span></div><span class="status">read only</span></header><div class="approvals-review-body"><h4>${esc(ticket.title || '(untitled)')}</h4><p>Independent review conversation. This Inbox does not add a verdict or reply endpoint.</p><p class="meta">${esc(ticket.status_label || ticket.status)} · updated ${esc(ticket.updated_at ? fmt(ticket.updated_at) : 'Not supplied')}</p></div><footer class="approvals-review-actions"><a class="primary-action" href="${ticketHref(central, board.board_id, ticket.id)}">Inspect evidence</a></footer></article>`;
  }

  function intakeRow({central, board}) {
    const {esc, boardHref} = context;
    return `<article class="approvals-intake-row" data-inbox-source="intake"><div><div class="approvals-route"><span>${esc(central)}</span><strong>${esc(board.label)}</strong></div><p>Approve or decline intake only inside this board's guarded source workflow.</p></div><a class="button" href="${boardHref(central, board.board_id)}">Open guarded intake</a></article>`;
  }

  function typedItems() {
    const {humanRequestRows, humanRequestCard, butlerHoldRows, butlerHoldCard, warmTickets, warmBoards} = context;
    const candidates = [];
    for (const row of humanRequestRows()) {
      const value = row.h || {}, identity = value.request_id || value.ticket_id || value['message'] || 'unknown';
      candidates.push({key:`human:${row.central}:${row.board.board_id}:${identity}`,type:'human-request',label:value.kind||'Human request',title:value['message']||'Human input requested',meta:`${row.central} · ${row.board.label}`,actionable:true,state:value.error?'error':'pending',render:()=>humanRequestCard(row)});
    }
    for (const row of butlerHoldRows()) {
      const value = row.f || {};
      candidates.push({key:`butler:${row.central}:${row.board.board_id}:${value.question_id||value.ticket_id||'unknown'}`,type:'butler-draft',label:'Butler draft',title:value.text||'Butler decision held for a human',meta:`${row.central} · ${row.board.label}`,actionable:true,state:value.hold?.status||'pending',render:()=>butlerHoldCard(row)});
    }
    for (const row of warmTickets().filter(({ticket}) => REVIEW_STATUSES.has(ticket.status))) {
      candidates.push({key:`review:${row.central}:${row.board.board_id}:${row.ticket.id}`,type:'review-conversation',label:'Review conversation',title:row.ticket.title||row.ticket.id,meta:`${row.central} · ${row.board.label}`,actionable:false,state:'read-only',render:()=>reviewRow(row)});
    }
    for (const row of warmBoards()) candidates.push({key:`intake:${row.central}:${row.board.board_id}`,type:'intake',label:'Intake queue',title:row.board.label,meta:`${row.central} · source workflow`,actionable:false,state:'guarded',render:()=>intakeRow(row)});
    const unique = new Map();
    for (const item of candidates) {
      const existing = unique.get(item.key);
      if (existing) existing.duplicateCount += 1;
      else unique.set(item.key, {...item, duplicateCount: 1});
    }
    return [...unique.values()];
  }

  function itemButton(item) {
    const {esc} = context, active = item.key === selectedKey;
    return `<button type="button" class="inbox-item" data-inbox-select="${esc(item.key)}" data-inbox-source="${esc(item.type)}" data-inbox-state="${esc(item.state)}" aria-pressed="${active}"><span><b>${esc(item.label)}</b><small>${esc(item.state)}${item.duplicateCount>1?` · ${esc(item.duplicateCount)} duplicate records collapsed`:''}</small></span><strong>${esc(item.title)}</strong><small>${esc(item.meta)}</small></button>`;
  }

  function evidenceGroups() {
    const {esc, butlerAgreementRows, butlerTicketAgreementRows, butlerRepeatedTicketRows, butlerEvaluationTruncationRows, butlerScoreCard, butlerRepeatedTicketCard} = context;
    const scores=butlerAgreementRows(), tickets=butlerTicketAgreementRows(), repeated=butlerRepeatedTicketRows(), truncated=butlerEvaluationTruncationRows();
    const warnings=truncated.map(row=>`<p class="warning">Bounded state omitted ${esc(row.count)} evaluation record(s) for ${esc(row.board.label)}.</p>`).join('');
    return `<details class="inbox-evidence"><summary>Butler marks and bounded conversation evidence</summary>${warnings}<section><h3>Agreement by question kind</h3>${scores.map(butlerScoreCard).join('')||'<p class="empty">No human marks yet.</p>'}</section><section><h3>Agreement by ticket</h3>${tickets.map(butlerScoreCard).join('')||'<p class="empty">No marked tickets yet.</p>'}</section><section><h3>Repeated questions</h3>${repeated.map(butlerRepeatedTicketCard).join('')||'<p class="empty">No repeated questions are present.</p>'}</section></details>`;
  }

  function renderWarmApprovals() {
    const {esc, pageHead, warmTruthStrip} = context;
    const items=typedItems(), actionableAll=items.filter(item=>item.actionable), historyAll=items.filter(item=>!item.actionable), actionable=actionableAll.slice(0,ACTIONABLE_LIMIT), history=historyAll.slice(0,HISTORY_LIMIT);
    if (!items.some(item=>item.key===selectedKey)) selectedKey=actionable[0]?.key||history[0]?.key||'';
    const selected=items.find(item=>item.key===selectedKey), mobile=typeof matchMedia==='function'&&matchMedia('(max-width: 800px)').matches;
    if (!mobile) detailOpen=true;
    return `${pageHead('Inbox','Decisions and conversations','Typed sources keep human requests, Butler drafts, intake, and read-only review conversations separate.')}${warmTruthStrip()}${sourceState()}<aside class="approvals-boundary"><strong>Actions stay source-specific.</strong><span>Human answers, Butler marks, intake decisions, and independent review retain their own permissions and handlers.</span></aside><section class="inbox-master-detail${detailOpen?' is-detail-open':''}" data-inbox-mobile-view="${detailOpen?'detail':'list'}"><div class="inbox-list" aria-label="Inbox items"><header><div><h3>Needs action</h3><p>${esc(actionableAll.length)} actionable; history is counted separately.</p></div><span class="status">${esc(actionable.length)} shown</span></header>${actionable.map(itemButton).join('')||'<p class="empty">No connected source is waiting for action.</p>'}${actionableAll.length>actionable.length?`<p class="warning">${esc(actionableAll.length-actionable.length)} actionable item(s) omitted by the bounded list.</p>`:''}<header><div><h3>History and guarded queues</h3><p>Read-only review conversations and intake entry points.</p></div><span class="status">${esc(history.length)} shown</span></header>${history.map(itemButton).join('')||'<p class="empty">No bounded history is available.</p>'}${historyAll.length>history.length?`<p class="warning">${esc(historyAll.length-history.length)} history item(s) omitted independently.</p>`:''}</div><div class="inbox-detail" aria-live="polite"><button type="button" class="button inbox-back" data-inbox-back>← Back to Inbox</button>${selected?`<header><span class="status">${esc(selected.label)}</span><h3>${esc(selected.title)}</h3><p>${esc(selected.meta)}</p></header>${selected.render()}`:'<p class="empty">Select an Inbox item.</p>'}</div></section>${evidenceGroups()}`;
  }

  function rerender(){const host=document.querySelector('#central-sections');if(host)host.innerHTML=renderWarmApprovals()}
  document.addEventListener('click',event=>{const select=event.target.closest('[data-inbox-select]');if(select&&document.querySelector('.inbox-master-detail')){selectedKey=select.dataset.inboxSelect;detailOpen=true;rerender();return}const back=event.target.closest('[data-inbox-back]');if(back&&document.querySelector('.inbox-master-detail')){detailOpen=false;rerender()}});

  loadStyles();
  globalThis.FleetViewModules.register({id:'approvals',owns:['typed Inbox','human requests','Butler drafts and marks','guarded intake','read-only review conversations'],render(context){useContext(context);return renderWarmApprovals()}});
})();
