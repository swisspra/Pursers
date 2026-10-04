/* Fleet route module: home. */
(function registerHomeView() {
  'use strict';

  let esc, fmt, centralHref, boardHref, ticketHref, pageHead, warmTruthStrip, warmBoards,
    warmTickets, warmNextAction, reconcileAttention, centralLabels, fleetData, fleetErrors,
    renderWaitingForYou, agentIdentity, agentDisplayState, workerForAgent, relativeAge;

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
    ({esc, fmt, centralHref, boardHref, ticketHref, pageHead, warmTruthStrip, warmBoards,
      warmTickets, warmNextAction, reconcileAttention, centralLabels, fleetData, fleetErrors = {},
      renderWaitingForYou, agentIdentity, agentDisplayState, workerForAgent, relativeAge} = context);
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

  function coveragePending() {
    return Object.values(fleetData).some(data => data?.refresh?.complete === false) ||
      warmBoards().some(item =>
        item?.board?.status === 'pending' ||
        item?.board?.coverage?.human_requests === 'pending'
      );
  }

  function renderHealthRow(label) {
    const data = fleetData[label];
    const summary = data?.pool_summary || {};
    const enriching = data?.refresh?.complete === false;
    const hasSummary = Number(data?.refresh?.covered_board_count || 0) > 0;
    const stale = Number(summary.stale || 0);
    const tone = !data ? 'danger' : enriching || stale ? 'warning' : 'good';
    const state = !data ? 'Reconnecting' : enriching ? 'Summary ready; enriching' : stale ? `${stale} stale` : 'Healthy';
    return `<a class="home-health-row" data-tone="${tone}" href="${esc(centralHref(label, 'overhead'))}">
      <span class="home-health-name"><span class="home-health-dot" aria-hidden="true"></span><b>${esc(label)}</b><span class="sr-only">${esc(state)}</span></span>
      <span class="home-health-counts">${data ? `${esc(enriching && !hasSummary ? '…' : summary.busy ?? '…')} working · ${esc(enriching && !hasSummary ? '…' : summary.available ?? '…')} ready${stale ? ` · ${esc(stale)} stale` : ''}${enriching ? ' · optional detail pending' : ''}` : state}</span>
    </a>`;
  }

  function attentionLink(item) {
    if (item.ticket_id) {
      return `<a class="id" href="${esc(ticketHref(item.central, item.board.board_id, item.ticket_id))}">${esc(item.ticket_id)}</a>`;
    }
    return `<a href="${esc(centralHref(item.central, 'overhead'))}">Inspect</a>`;
  }

  function renderOperationalAttention(items, pending) {
    if (!items.length) {
      if (pending) {
        return `<section class="home-attention" data-state="pending" aria-label="Attention status" aria-busy="true">
          <div><h3>Checking remaining attention</h3><p class="muted">Available summaries are visible. Human requests, history, and slow projects are still loading; no zero or calm state is assumed.</p></div>
          <a href="#/projects">View available projects</a>
        </section>`;
      }
      return `<section class="home-attention" data-state="calm" aria-label="Attention status">
        <span class="home-calm-mark" aria-hidden="true">✓</span>
        <div><h3>No operational blockers</h3><p class="muted">No unacknowledged Fleet signal needs intervention in this bounded view.</p></div>
        <a href="#/activity">View activity</a>
      </section>`;
    }
    const visible = items.slice(0, 4);
    return `<section class="home-attention" data-state="active" aria-labelledby="home-attention-title">
      <div class="home-panel-head"><h3 id="home-attention-title">Needs you</h3><span class="status" data-tone="danger">${esc(items.length)} surfaced</span></div>
      <div class="home-attention-list">${visible.map(item => `<div class="home-attention-row">
        <span class="home-severity ${item.level === 'critical' ? 'critical' : ''}" aria-hidden="true"></span>
        <div><b>${esc(item.title)}</b><p>${esc(item.text)}</p><span class="meta">${esc(item.central)} · ${esc(item.board.label)} · first seen ${esc(fmt(item.first_seen))}</span><div class="attention-actions">${item.ask_id?`<button type="button" data-intake-decline data-central="${esc(item.central)}" data-board="${esc(item.board.board_id)}" data-ask-id="${esc(item.ask_id)}">Decline</button><span role="status" aria-live="polite" data-intake-result></span>`:''}<button type="button" data-attention-action="ack" data-attention-key="${esc(item.key)}">Acknowledge</button><button type="button" data-attention-action="snooze" data-attention-key="${esc(item.key)}">Snooze 24h</button></div></div>
        ${attentionLink(item)}
      </div>`).join('')}</div>
      ${items.length > visible.length ? `<p class="meta">${esc(items.length - visible.length)} more source-backed signals remain after this one-glance view.</p>` : ''}
    </section>`;
  }

  function greeting() {
    const hour = new Date().getHours();
    if (hour < 12) return 'Good morning';
    if (hour < 18) return 'Good afternoon';
    return 'Good evening';
  }

  function floorRows() {
    const rows = [];
    const seen = new Set();
    for (const [central, data] of Object.entries(fleetData)) {
      for (const agent of data?.agents || []) {
        const identity = agentIdentity(agent);
        if (seen.has(identity)) continue;
        seen.add(identity);
        const managed = workerForAgent(central, agent);
        const state = agentDisplayState(agent, managed);
        const seats = agent.seats || [];
        const active = seats.find(seat => seat.current_ticket_id);
        const role = seats.find(seat => seat.role)?.role || managed?.role || 'agent';
        rows.push({central, agent, state, active, role});
      }
    }
    const rank = {working: 0, available: 1, connected: 2, stale: 3, offline: 4};
    return rows.sort((a, b) => (rank[a.state] ?? 5) - (rank[b.state] ?? 5) ||
      String(a.agent.agent_name || '').localeCompare(String(b.agent.agent_name || '')));
  }

  function renderFloor(pending) {
    const rows = floorRows();
    const visible = rows.slice(0, 6);
    const stateLabels = {working: 'Working', available: 'Ready', connected: 'Connected', stale: 'Stale', offline: 'Offline'};
    const body = visible.length ? visible.map(({central, agent, state, active, role}) => {
      const work = active
        ? `<a class="id" href="${esc(ticketHref(central, active.board_id, active.current_ticket_id))}">${esc(active.current_ticket_id)}</a>`
        : `<span class="muted">${state === 'available' ? 'No held ticket' : 'No current claim reported'}</span>`;
      return `<li class="home-floor-row" data-state="${esc(state)}">
        <span class="home-avatar" aria-hidden="true">${esc(String(agent.agent_name || '?').slice(0, 1).toUpperCase())}</span>
        <div><b>${esc(agent.display_name || agent.agent_name || 'Unnamed seat')}</b><span class="meta">${esc(role)} · ${esc(central)}</span>${work}</div>
        <span class="home-floor-state"><span class="home-floor-dot" aria-hidden="true"></span>${esc(stateLabels[state] || 'Unknown')}<small>${esc(relativeAge(agent.last_seen))}</small></span>
      </li>`;
    }).join('') : `<li class="home-floor-empty">${pending ? 'Seat details are still enriching. Available work summaries remain usable.' : 'No active seats are reported in this bounded view.'}</li>`;
    return `<section class="home-floor" aria-labelledby="home-floor-title">
      <div class="home-panel-head"><h3 id="home-floor-title">On the floor</h3><a href="#/team">All staff</a></div>
      <ul>${body}</ul>
      ${rows.length > visible.length ? `<p class="meta">${esc(rows.length - visible.length)} more observed seats are available in Team.</p>` : ''}
    </section>`;
  }

  function renderWarmHome() {
    const boards = warmBoards();
    if (!boards.length) {
      const unavailable = centralLabels.filter(label => !fleetData[label] && fleetErrors[label]);
      if (unavailable.length) {
        const detail = unavailable.map(label => `${label} (${fleetErrors[label]})`).join(', ');
        return `${pageHead('Home', 'Sources unavailable', 'Fleet could not load a source-backed snapshot. Navigation and local preferences remain available.')}${warmTruthStrip()}<section class="home-attention" data-state="error" role="alert">
          <span class="home-severity critical" aria-hidden="true"></span>
          <div><h3>Central snapshot unavailable</h3><p class="muted">${esc(detail)} did not return Fleet data. Check the Central URL and token scope, then retry; no zero or healthy state is inferred.</p></div>
          <a href="#/settings">Open Settings</a>
        </section>`;
      }
      const waitingForCentral = centralLabels.some(label => !fleetData[label]);
      if (waitingForCentral) {
        return `${pageHead('Home', 'Building your team view', 'Connecting to each Central before showing source-backed work and health.')}${warmTruthStrip()}<section class="home-loading" aria-label="Loading team status" aria-busy="true"><div class="skeleton"></div><div class="skeleton"></div></section>`;
      }
      return `${pageHead('Home', 'Welcome to your work home', 'Connect one project, then describe work and let the board route it safely.')}${warmTruthStrip()}<section class="empty-guidance"><h3>Connect your first project</h3><p>Add a project in Settings. Pursers will prepare its board, protected doors, and isolated Fleet clone.</p><div class="card-actions"><a class="primary-action" href="#/settings">Open Settings</a></div></section>`;
    }

    const tickets = warmTickets();
    const operationalAttention = reconcileAttention().sort((a, b) =>
      (b.level === 'critical') - (a.level === 'critical') || (b.age || 0) - (a.age || 0)
    );
    const humanPending = pendingHumanCount();
    const pending = coveragePending();
    const working = tickets.filter(item => ['claimed', 'in_progress', 'creating_report'].includes(item.ticket.status)).length;
    const blocked = tickets.filter(item => item.ticket.status === 'needs_human' || item.ticket.parked === true).length;
    const submitted = tickets.filter(item => item.ticket.status === 'submitted').length;
    const open = tickets.filter(item => item.ticket.status === 'open').length;
    const readyForTeam = tickets.filter(item => item.ticket.delivery?.state === 'integration_merged').length;
    const attention = humanPending + operationalAttention.length;
    const observed = value => pending ? `≥${value}` : value;

    const only = boards.length === 1 ? boards[0] : null;
    const intentHref = only ? boardHref(only.central, only.board.board_id) : '#/projects';
    const headActions = `<div class="home-head-actions"><a class="button" href="#/inbox">Open Inbox${humanPending ? ` · ${esc(humanPending)}` : ''}</a><a class="primary-action" href="${esc(intentHref)}">${only ? 'New intent' : 'Choose project'}</a></div>`;

    return `${pageHead('Home', greeting(), `${boards.length} connected ${boards.length === 1 ? 'project' : 'projects'} · ${pending ? 'bounded summaries ready; optional detail enriching' : 'bounded source data current'}`, headActions)}${warmTruthStrip()}
      <dl class="home-status-grid" aria-label="Bounded work status">
        <div><dt>In progress</dt><dd>${esc(observed(working))}</dd><span>Claimed or actively reporting</span></div>
        <div><dt>Review ready</dt><dd>${esc(observed(submitted))}</dd><span>Awaiting independent review</span></div>
        <div class="${blocked ? 'is-alert' : ''}"><dt>Blocked</dt><dd>${esc(observed(blocked))}</dd><span>Needs human or parked</span></div>
        <div><dt>Open queue</dt><dd>${esc(observed(open))}</dd><span>Visible work not yet claimed</span></div>
      </dl>
      <section class="home-source-status" aria-label="Central status"><div class="home-panel-head"><h3>Source status</h3><span class="home-scope">${esc(tickets.length)} visible tickets</span></div><div class="home-health-list">${centralLabels.map(renderHealthRow).join('')}</div></section>
      ${readyForTeam ? `<section class="home-human-queue"><h3>Ready for your team</h3><p>${esc(readyForTeam)} visible ticket${readyForTeam === 1 ? '' : 's'} confirmed on the delivery branch. Your team handles the final merge.</p><a href="#/work">Inspect delivered work</a></section>` : ''}
      <div class="home-operational-grid">${renderOperationalAttention(operationalAttention, pending)}${renderFloor(pending)}</div>
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
