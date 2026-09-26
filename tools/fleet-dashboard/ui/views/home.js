/* Fleet route module: home. */
(function registerHomeView() {
  'use strict';
  let esc, pageHead, warmTruthStrip, warmBoards, warmTickets, warmNextAction,
    reconcileAttention, centralLabels, fleetData, renderWaitingForYou;
  function useContext(context) {
    ({esc, pageHead, warmTruthStrip, warmBoards, warmTickets, warmNextAction,
      reconcileAttention, centralLabels, fleetData, renderWaitingForYou} = context);
  }
  function renderWarmHome(){const boards=warmBoards(),tickets=warmTickets();if(!boards.length)return `${pageHead('Home','Welcome to your work home','Connect one project, then describe work and let the board route it safely.')}${warmTruthStrip()}<section class="empty-guidance"><h3>Connect your first project</h3><p>Add a project in Settings. Pursers will prepare its board, protected doors, and isolated Fleet clone.</p><div class="card-actions"><a class="primary-action" href="#/settings">Open Settings</a></div></section>`;const next=warmNextAction(),active=tickets.filter(item=>['claimed','in_progress','creating_report'].includes(item.ticket.status)).length,submitted=tickets.filter(item=>item.ticket.status==='submitted').length,attention=reconcileAttention().length,hero=next?`<article class="guided-next"><p class="eyebrow">${esc(next.eyebrow)}</p><h3>${esc(next.title)}</h3><p>${esc(next.copy)}</p><div class="card-actions"><a class="primary-action" href="${esc(next.href)}">${esc(next.label)}</a><a href="#/work">View all work</a></div></article>`:'';return `${pageHead('Home','Good to see your Team','Start with the next safe action, then scan project health and progress.')}${warmTruthStrip()}<section class="guided-hero">${hero}<div class="guided-summary"><div><span>Projects</span><b>${boards.length}</b></div><div><span>Working</span><b>${active}</b></div><div><span>Review ready</span><b>${submitted}</b></div><div><span>Needs attention</span><b>${attention}</b></div></div></section><section class="warm-grid">${centralLabels.map(label=>{const data=fleetData[label],summary=data?.pool_summary||{};return `<article class="health-card"><div class="signal"><span class="signal-dot ${data?'':'bad'}"></span><b>${esc(label)}</b><span class="status">${data?'connected':'reconnecting'}</span></div><p class="meta">${data?'Independent central trust domain':'Last-known data stays labeled stale.'}</p><div class="health-metrics"><span>Working<b>${esc(summary.busy||0)}</b></span><span>Ready<b>${esc(summary.available||0)}</b></span><span>Stale<b>${esc(summary.stale||0)}</b></span></div></article>`}).join('')}</section>${renderWaitingForYou()}`}
  globalThis.FleetViewModules.register({
    id: 'home',
    owns: [
      'fleet health summary',
    'attention queue',
    'guided start'
    ],
    render(context) { useContext(context); return renderWarmHome(); }
  });
})();
