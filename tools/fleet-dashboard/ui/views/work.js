/* Fleet route module: work. */
(function registerWorkView() {
  'use strict';
  let esc, pageHead, warmTruthStrip, warmTickets, ticketHref, boardHref;
  function useContext(context) {
    ({esc, pageHead, warmTruthStrip, warmTickets, ticketHref, boardHref} = context);
  }
  function renderWarmWork(){const groups=['open','claimed','in_progress','creating_report','submitted','needs_human','closed','rejected','canceled','terminated'],tickets=warmTickets(),known=new Set(groups),sections=[];for(const status of groups){const items=tickets.filter(item=>item.ticket.status===status);if(!items.length)continue;sections.push(`<section class="card warm-section"><div class="section-title"><h3>${esc(status.replaceAll('_',' '))}</h3><span class="status">${items.length}</span></div><div class="warm-list">${items.map(({central,board,ticket})=>`<div class="warm-row"><div><a class="id" href="${ticketHref(central,board.board_id,ticket.id)}">${esc(ticket.id)}</a><b>${esc(ticket.title||'(untitled)')}</b><p class="meta">${esc(board.label)} · ${esc(central)} · ${esc(ticket.claimed_by||'unclaimed')}</p></div><div class="warm-row-actions"><a class="button" href="${ticketHref(central,board.board_id,ticket.id)}">Details</a><a class="button" href="${boardHref(central,board.board_id,'flow')}">Flow</a></div></div>`).join('')}</div></section>`)}const unknown=tickets.filter(item=>!known.has(item.ticket.status));if(unknown.length)sections.push(`<section class="card warm-section"><div class="section-title"><h3>Other states</h3><span class="status">${unknown.length}</span></div><p class="muted">Unknown states remain visible rather than being silently grouped.</p>${unknown.map(({central,board,ticket})=>`<div class="warm-row"><a class="id" href="${ticketHref(central,board.board_id,ticket.id)}">${esc(ticket.id)}</a><span class="status">${esc(ticket.status)}</span></div>`).join('')}</section>`);return `${pageHead('Work','Work across every project','Follow open, active, review-ready, completed, ended, and unknown ticket states. Ticket detail preserves timeline, changes, flow, routes, leases, and handoffs.')}${warmTruthStrip()}${sections.join('')||'<div class="empty-guidance"><h3>No work yet</h3><p>Choose a project and use its guarded intake to describe the outcome you need.</p><div class="card-actions"><a class="primary-action" href="#/projects">Choose a project</a></div></div>'}`}
  globalThis.FleetViewModules.register({
    id: 'work',
    owns: [
      'ticket work queue',
    'ticket filters',
    'work empty states'
    ],
    render(context) { useContext(context); return renderWarmWork(); }
  });
})();
