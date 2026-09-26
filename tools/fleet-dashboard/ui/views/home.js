/* Fleet route module: home. */
(function registerHomeView() {
  'use strict';

  let esc, fmt, centralHref, ticketHref, pageHead, warmTruthStrip, warmBoards,
    warmTickets, warmNextAction, reconcileAttention, centralLabels, fleetData,
    renderWaitingForYou;

  const HOME_STYLES = `<style>
    .home-briefing{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(320px,.95fr);gap:18px;align-items:stretch;margin-bottom:22px}
    .home-command,.home-pulse,.home-attention{min-width:0;border:1px solid var(--line);border-radius:22px;background:var(--panel);box-shadow:var(--shadow)}
    .home-command{display:flex;min-height:285px;flex-direction:column;justify-content:space-between;padding:clamp(24px,4vw,42px);background:var(--warm-action);border-color:color-mix(in srgb,var(--accent) 28%,var(--line))}
    .home-command[data-pressure="high"]{background:var(--warm-danger);border-color:color-mix(in srgb,var(--bad) 36%,var(--line))}
    .home-command-copy{display:grid;gap:12px;max-width:62ch}
    .home-kicker{color:var(--accent);font-size:13px;font-weight:800}
    .home-command[data-pressure="high"] .home-kicker{color:var(--bad)}
    .home-command h3{font-size:clamp(28px,4vw,46px);line-height:1.05;letter-spacing:-.035em;overflow-wrap:anywhere}
    .home-command-copy>p:last-child{color:var(--text);font-size:15px}
    .home-command .card-actions{justify-content:flex-start;margin-top:24px}
    .home-command .card-actions a{min-height:44px}
    .home-pulse{display:grid;align-content:start;padding:22px}
    .home-panel-head{display:flex;align-items:baseline;justify-content:space-between;gap:12px;padding-bottom:14px;border-bottom:1px solid var(--line)}
    .home-panel-head h3{font-size:18px}
    .home-scope{color:var(--muted);font-size:12px;text-align:right}
    .home-stats{display:grid;grid-template-columns:1fr 1fr;margin:0;padding:8px 0 12px}
    .home-stats>div{display:grid;grid-template-columns:1fr auto;align-items:baseline;gap:10px;padding:10px 8px;border-bottom:1px solid var(--line)}
    .home-stats>div:nth-child(odd){padding-left:0;padding-right:16px;border-right:1px solid var(--line)}
    .home-stats>div:nth-child(even){padding-left:16px;padding-right:0}
    .home-stats dt{color:var(--muted);font-size:12px}
    .home-stats dd{margin:0;font-size:21px;font-weight:800}
    .home-stats .is-alert dd{color:var(--bad)}
    .home-health-list{display:grid;gap:2px;margin-top:2px}
    .home-health-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;align-items:center;min-height:44px;padding:10px 0;border-bottom:1px solid var(--line);color:var(--text)}
    .home-health-row:last-child{border-bottom:0}
    .home-health-row:hover{text-decoration:none}
    .home-health-name{display:flex;min-width:0;align-items:center;gap:8px}
    .home-health-name b{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .home-health-counts{color:var(--muted);font-size:12px;text-align:right;white-space:nowrap}
    .home-health-dot{width:9px;height:9px;flex:0 0 auto;border-radius:50%;background:var(--good);box-shadow:0 0 0 4px color-mix(in srgb,var(--good) 14%,transparent)}
    .home-health-row[data-tone="warning"] .home-health-dot{background:var(--warn);box-shadow:0 0 0 4px color-mix(in srgb,var(--warn) 15%,transparent)}
    .home-health-row[data-tone="danger"] .home-health-dot{background:var(--bad);box-shadow:0 0 0 4px color-mix(in srgb,var(--bad) 15%,transparent)}
    .home-health-row[data-tone="danger"] .home-health-counts{color:var(--bad)}
    .home-attention{margin:0 0 22px;padding:22px}
    .home-attention[data-state="calm"]{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:12px;align-items:center;box-shadow:none}
    .home-attention[data-state="calm"]>a,.home-attention-row>a{display:inline-flex;min-height:44px;align-items:center}
    .home-calm-mark{display:grid;place-items:center;width:30px;height:30px;border-radius:50%;background:var(--warm-action);color:var(--accent);font-weight:900}
    .home-attention-list{display:grid}
    .home-attention-row{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:12px;align-items:start;padding:16px 0;border-top:1px solid var(--line)}
    .home-attention-row:first-child{border-top:0}
    .home-attention-row p{margin-top:4px;color:var(--muted)}
    .home-attention-row .meta{display:block;margin-top:6px}
    .home-attention-row .attention-actions button{min-height:44px}
    .home-severity{width:9px;height:9px;margin-top:6px;border-radius:50%;background:var(--warn)}
    .home-severity.critical{background:var(--bad)}
    .home-human-queue{margin-top:28px}
    .home-human-queue>.section-title:first-child{margin-top:0}
    .home-human-queue .attention-actions button,.home-human-queue .button,.home-human-queue select,.home-human-queue .finding-row>a{min-height:44px}
    .home-human-queue .finding-row>a{display:inline-flex;align-items:center}
    .home-loading{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(260px,.8fr);gap:18px}
    .home-loading .skeleton{min-height:210px}
    :root[data-density="compact"] .home-command{min-height:230px;padding:24px}
    :root[data-density="compact"] .home-pulse,:root[data-density="compact"] .home-attention{padding:16px}
    @media(max-width:980px){.home-briefing,.home-loading{grid-template-columns:1fr}.home-command{min-height:0}.home-pulse{grid-template-columns:minmax(0,1fr)}.home-health-list{grid-template-columns:1fr 1fr;column-gap:18px}}
    @media(max-width:560px){.home-command{padding:22px}.home-command h3{font-size:30px}.home-pulse{padding:18px}.home-stats{grid-template-columns:1fr}.home-stats>div,.home-stats>div:nth-child(odd),.home-stats>div:nth-child(even){padding:10px 0;border-right:0}.home-health-list{grid-template-columns:1fr}.home-attention{padding:18px}.home-attention[data-state="calm"]{grid-template-columns:auto minmax(0,1fr)}.home-attention[data-state="calm"]>a{grid-column:2}.home-attention-row{grid-template-columns:auto minmax(0,1fr)}.home-attention-row>a{grid-column:2}.home-scope{max-width:17ch}}
    @media(forced-colors:active){.home-command,.home-pulse,.home-attention{border:1px solid CanvasText}.home-health-dot,.home-severity{border:1px solid CanvasText}}
  </style>`;

  function useContext(context) {
    ({esc, fmt, centralHref, ticketHref, pageHead, warmTruthStrip, warmBoards,
      warmTickets, warmNextAction, reconcileAttention, centralLabels, fleetData,
      renderWaitingForYou} = context);
  }

  function pendingHumanCount() {
    let count = 0;
    for (const data of Object.values(fleetData)) {
      for (const board of data?.boards || []) {
        count += (board.human_requests || []).length;
        count += (board.coordinator_findings?.items || []).filter(item =>
          item.kind === 'would_answer' && item.question_id &&
          ['shadow', 'pending'].includes(item.hold?.status)
        ).length;
      }
    }
    return count;
  }

  function renderHealthRow(label) {
    const data = fleetData[label];
    const summary = data?.pool_summary || {};
    const stale = Number(summary.stale || 0);
    const tone = !data ? 'danger' : stale ? 'warning' : 'good';
    const state = !data ? 'Reconnecting' : stale ? `${stale} stale` : 'Healthy';
    return `<a class="home-health-row" data-tone="${tone}" href="${esc(centralHref(label, 'overhead'))}">
      <span class="home-health-name"><span class="home-health-dot" aria-hidden="true"></span><b>${esc(label)}</b><span class="sr-only">${esc(state)}</span></span>
      <span class="home-health-counts">${data ? `${esc(summary.busy || 0)} working · ${esc(summary.available || 0)} ready${stale ? ` · ${esc(stale)} stale` : ''}` : state}</span>
    </a>`;
  }

  function attentionLink(item) {
    if (item.ticket_id) {
      return `<a class="id" href="${esc(ticketHref(item.central, item.board.board_id, item.ticket_id))}">${esc(item.ticket_id)}</a>`;
    }
    return `<a href="${esc(centralHref(item.central, 'overhead'))}">Inspect</a>`;
  }

  function renderOperationalAttention(items) {
    if (!items.length) {
      return `<section class="home-attention" data-state="calm" aria-label="Attention status">
        <span class="home-calm-mark" aria-hidden="true">✓</span>
        <div><h3>No operational blockers</h3><p class="muted">No unacknowledged Fleet signal needs intervention in this bounded view.</p></div>
        <a href="#/activity">View activity</a>
      </section>`;
    }
    const visible = items.slice(0, 4);
    return `<section class="home-attention" data-state="active" aria-labelledby="home-attention-title">
      <div class="home-panel-head"><h3 id="home-attention-title">Needs attention now</h3><span class="status" data-tone="danger">${esc(items.length)} surfaced</span></div>
      <div class="home-attention-list">${visible.map(item => `<div class="home-attention-row">
        <span class="home-severity ${item.level === 'critical' ? 'critical' : ''}" aria-hidden="true"></span>
        <div><b>${esc(item.title)}</b><p>${esc(item.text)}</p><span class="meta">${esc(item.central)} · ${esc(item.board.label)} · first seen ${esc(fmt(item.first_seen))}</span><div class="attention-actions"><button type="button" data-attention-action="ack" data-attention-key="${esc(item.key)}">Acknowledge</button><button type="button" data-attention-action="snooze" data-attention-key="${esc(item.key)}">Snooze 24h</button></div></div>
        ${attentionLink(item)}
      </div>`).join('')}</div>
      ${items.length > visible.length ? `<p class="meta">${esc(items.length - visible.length)} more source-backed signals remain after this one-glance view.</p>` : ''}
    </section>`;
  }

  function renderWarmHome() {
    const boards = warmBoards();
    if (!boards.length) {
      const waitingForCentral = centralLabels.some(label => !fleetData[label]);
      if (waitingForCentral) {
        return `${HOME_STYLES}${pageHead('Home', 'Building your team view', 'Connecting to each Central before showing source-backed work and health.')}${warmTruthStrip()}<section class="home-loading" aria-label="Loading team status" aria-busy="true"><div class="skeleton"></div><div class="skeleton"></div></section>`;
      }
      return `${HOME_STYLES}${pageHead('Home', 'Welcome to your work home', 'Connect one project, then describe work and let the board route it safely.')}${warmTruthStrip()}<section class="empty-guidance"><h3>Connect your first project</h3><p>Add a project in Settings. Pursers will prepare its board, protected doors, and isolated Fleet clone.</p><div class="card-actions"><a class="primary-action" href="#/settings">Open Settings</a></div></section>`;
    }

    const tickets = warmTickets();
    const next = warmNextAction();
    const operationalAttention = reconcileAttention().sort((a, b) =>
      (b.level === 'critical') - (a.level === 'critical') || (b.age || 0) - (a.age || 0)
    );
    const humanPending = pendingHumanCount();
    const working = tickets.filter(item => ['claimed', 'in_progress', 'creating_report'].includes(item.ticket.status)).length;
    const blocked = tickets.filter(item => item.ticket.status === 'needs_human' || item.ticket.parked === true).length;
    const submitted = tickets.filter(item => item.ticket.status === 'submitted').length;
    const open = tickets.filter(item => item.ticket.status === 'open').length;
    const attention = humanPending + operationalAttention.length;
    const pressure = attention || blocked ? 'high' : 'calm';

    const command = next ? `<article class="home-command" data-pressure="${pressure}">
      <div class="home-command-copy"><p class="home-kicker">${esc(next.eyebrow)}</p><h3>${esc(next.title)}</h3><p>${esc(next.copy)}</p></div>
      <div class="card-actions"><a class="primary-action" href="${esc(next.href)}">${esc(next.label)}</a><a href="#/work">View all work</a></div>
    </article>` : '';

    return `${HOME_STYLES}${pageHead('Home', 'Your team, at a glance', 'See what needs judgment, what is moving, what is blocked, and the next safe action.')}${warmTruthStrip()}
      <section class="home-briefing">${command}<aside class="home-pulse" aria-labelledby="home-team-status">
        <div class="home-panel-head"><h3 id="home-team-status">Team status</h3><span class="home-scope">${esc(boards.length)} projects · ${esc(tickets.length)} visible tickets · bounded</span></div>
        <dl class="home-stats"><div><dt>In progress</dt><dd>${esc(working)}</dd></div><div class="${blocked ? 'is-alert' : ''}"><dt>Blocked</dt><dd>${esc(blocked)}</dd></div><div><dt>Review ready</dt><dd>${esc(submitted)}</dd></div><div><dt>Open queue</dt><dd>${esc(open)}</dd></div></dl>
        <div class="home-health-list" aria-label="Central health">${centralLabels.map(renderHealthRow).join('')}</div>
      </aside></section>
      ${renderOperationalAttention(operationalAttention)}
      ${humanPending ? `<section class="home-human-queue" aria-label="Human decision queue">${renderWaitingForYou()}</section>` : ''}`;
  }

  globalThis.FleetViewModules.register({
    id: 'home',
    owns: [
      'fleet health summary',
      'attention queue',
      'guided start'
    ],
    render(context) {
      useContext(context);
      return renderWarmHome();
    }
  });
})();
