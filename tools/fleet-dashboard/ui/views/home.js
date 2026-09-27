/* Fleet route module: home. */
(function registerHomeView() {
  'use strict';

  let esc, fmt, centralHref, ticketHref, pageHead, warmTruthStrip, warmBoards,
    warmTickets, warmNextAction, reconcileAttention, centralLabels, fleetData,
    renderWaitingForYou;

  const STYLE_URL = '/ui/views/home.css';

  function loadStyles() {
    if (typeof document === 'undefined') return;
    if (document.querySelector('link[data-fleet-view-style="home"]')) return;
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = STYLE_URL;
    link.dataset.fleetViewStyle = 'home';
    document.head.append(link);
  }

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
        return `${pageHead('Home', 'Building your team view', 'Connecting to each Central before showing source-backed work and health.')}${warmTruthStrip()}<section class="home-loading" aria-label="Loading team status" aria-busy="true"><div class="skeleton"></div><div class="skeleton"></div></section>`;
      }
      return `${pageHead('Home', 'Welcome to your work home', 'Connect one project, then describe work and let the board route it safely.')}${warmTruthStrip()}<section class="empty-guidance"><h3>Connect your first project</h3><p>Add a project in Settings. Pursers will prepare its board, protected doors, and isolated Fleet clone.</p><div class="card-actions"><a class="primary-action" href="#/settings">Open Settings</a></div></section>`;
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

    return `${pageHead('Home', 'Your team, at a glance', 'See what needs judgment, what is moving, what is blocked, and the next safe action.')}${warmTruthStrip()}
      <section class="home-briefing">${command}<aside class="home-pulse" aria-labelledby="home-team-status">
        <div class="home-panel-head"><h3 id="home-team-status">Team status</h3><span class="home-scope">${esc(boards.length)} projects · ${esc(tickets.length)} visible tickets · bounded</span></div>
        <dl class="home-stats"><div><dt>In progress</dt><dd>${esc(working)}</dd></div><div class="${blocked ? 'is-alert' : ''}"><dt>Blocked</dt><dd>${esc(blocked)}</dd></div><div><dt>Review ready</dt><dd>${esc(submitted)}</dd></div><div><dt>Open queue</dt><dd>${esc(open)}</dd></div></dl>
        <div class="home-health-list" aria-label="Central health">${centralLabels.map(renderHealthRow).join('')}</div>
      </aside></section>
      ${renderOperationalAttention(operationalAttention)}
      ${humanPending ? `<section class="home-human-queue" aria-label="Human decision queue">${renderWaitingForYou()}</section>` : ''}`;
  }

  loadStyles();
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
