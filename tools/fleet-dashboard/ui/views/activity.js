/* Fleet route module: activity. */
(function registerActivityView() {
  'use strict';
  let esc, fmt, pageHead, warmTruthStrip, warmTickets, ticketHref, boardHref,
    warmBoards, autonomousRows, autonomousStateLabel;
  function useContext(context) {
    ({esc, fmt, pageHead, warmTruthStrip, warmTickets, ticketHref, boardHref,
      warmBoards, autonomousRows, autonomousStateLabel} = context);
  }
  function renderWarmActivity(){const recent=warmTickets().filter(item=>item.ticket.updated_at).sort((a,b)=>new Date(b.ticket.updated_at)-new Date(a.ticket.updated_at)).slice(0,20),rows=recent.map(({central,board,ticket})=>`<div class="warm-row"><div><b>${esc(ticket.title||ticket.id)}</b><p class="meta">${esc(fmt(ticket.updated_at))} · ${esc(ticket.status)} · ${esc(board.label)}</p></div><div class="warm-row-actions"><a class="button" href="${ticketHref(central,board.board_id,ticket.id)}">Ticket</a><a class="button" href="${boardHref(central,board.board_id,'timeline')}">Timeline</a><a class="button" href="${boardHref(central,board.board_id,'changes')}">Changes</a></div></div>`).join('');return `${pageHead('Activity','Bounded activity','Recent ticket state comes from the bounded Fleet snapshot. Open a board timeline for actor and event provenance; older events may exist.')}${warmTruthStrip()}<section class="card"><div class="section-title"><h3>Recent state changes</h3><span class="status">up to 20 shown</span></div><div class="warm-list">${rows||'<p class="empty">No recent ticket state is visible.</p>'}</div></section><section class="warm-grid warm-section">${warmBoards().map(({central,board})=>`<article class="card"><p class="eyebrow">${esc(central)}</p><h3>${esc(board.label)}</h3><p class="muted">Actor provenance, cursor, has-more, dropped, and resync state remain in board detail.</p><div class="card-actions"><a href="${boardHref(central,board.board_id,'timeline')}">Timeline</a><a href="${boardHref(central,board.board_id,'changes')}">Changes</a><a href="${boardHref(central,board.board_id,'routes')}">Routes</a></div></article>`).join('')}</section>`}
  function renderAutonomousActivity(){const cards=autonomousRows().filter(row=>row.data).map(({central,board,data})=>`<article class="card"><div class="section-title"><div><p class="eyebrow">${esc(board.label)} · ${esc(central)}</p><h3>Butler command and audit history</h3></div><span class="status">${esc(data.commands?.length||0)} shown</span></div><div class="autonomous-history">${(data.commands||[]).map(command=>`<div class="warm-row"><div><b>${esc(command.intent||'unknown')}</b><p class="meta">${esc(command.command_id||'—')} · revision ${esc(command.revision||'—')} · ${esc(fmt(command.updated_at))}</p><p class="meta">Audit ${esc(command.audit_id||'unavailable')} · ${esc(command.reason_code||'no reason code')}</p></div><span class="status" data-tone="${['failed','rejected','cancelled'].includes(command.status)?'danger':'ready'}">${esc(command.status||'unknown')}</span></div>`).join('')||'<p class="empty">No commands are visible.</p>'}</div>${data.history_truncated?'<p class="warning">History is bounded; older commands exist.</p>':''}</article>`).join('');return `<section class="autonomous-grid" aria-label="Autonomous Butler activity">${cards}</section>`}
  globalThis.FleetViewModules.register({
    id: 'activity',
    owns: [
      'recent activity',
    'butler activity',
    'activity empty states'
    ],
    render(context) { useContext(context); return renderWarmActivity() + renderAutonomousActivity(); }
  });
})();
