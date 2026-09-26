/* Fleet route module: approvals. */
(function registerApprovalsView() {
  'use strict';
  let esc, pageHead, warmTruthStrip, warmTickets, ticketHref, warmBoards,
    boardHref, renderWaitingForYou;
  function useContext(context) {
    ({esc, pageHead, warmTruthStrip, warmTickets, ticketHref, warmBoards,
      boardHref, renderWaitingForYou} = context);
  }
  function renderWarmApprovals(){const submitted=warmTickets().filter(item=>item.ticket.status==='submitted'),reviewRows=submitted.map(({central,board,ticket})=>`<div class="warm-row"><div><a class="id" href="${ticketHref(central,board.board_id,ticket.id)}">${esc(ticket.id)}</a><b>${esc(ticket.title||'(untitled)')}</b><p class="meta">${esc(board.label)} · independent review required</p></div><div class="warm-row-actions"><a class="button" href="${ticketHref(central,board.board_id,ticket.id)}">Inspect evidence</a></div></div>`).join(''),intakeLinks=warmBoards().map(({central,board})=>`<a class="button" href="${boardHref(central,board.board_id)}">${esc(board.label)} intake</a>`).join('');return `${pageHead('Approvals','Decisions that need judgment','Human requests, submitted review, and guarded intake stay distinct. Destructive actions keep their confirmation boundaries.')}${warmTruthStrip()}${renderWaitingForYou()}<section class="card warm-section"><div class="section-title"><h3>Submitted for independent review</h3><span class="status">${submitted.length}</span></div><div class="warm-list">${reviewRows||'<p class="empty">Nothing is waiting for review.</p>'}</div></section><section class="card warm-section"><div class="section-title"><h3>Intake queues</h3><span class="status">source-backed</span></div><p class="muted">Open a project to view, approve, decline, or create intake through the guarded endpoint.</p><div class="card-actions">${intakeLinks||'<span class="empty">No project intake queues.</span>'}</div></section>`}
  globalThis.FleetViewModules.register({
    id: 'approvals',
    owns: [
      'approval queue',
    'human requests',
    'approval empty states'
    ],
    render(context) { useContext(context); return renderWarmApprovals(); }
  });
})();
