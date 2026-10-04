/* Fleet route module: work. */
(function registerWorkView() {
  'use strict';

  const statusGroups = [
    {key: 'attention', label: 'Needs attention', statuses: ['needs_human', 'rejected']},
    {key: 'review', label: 'Being reviewed', statuses: ['submitted', 'reviewing', 'in_review']},
    {key: 'working', label: 'In progress', statuses: ['claimed', 'in_progress', 'creating_report']},
    {key: 'finding', label: 'Finding a teammate', statuses: ['open', 'offered', 'assigned']},
    {key: 'complete', label: 'Review approved', statuses: ['closed']},
    {key: 'ended', label: 'Ended', statuses: ['canceled', 'terminated']},
  ];
  const knownStatuses = new Set(statusGroups.flatMap(group => group.statuses));
  let activeFilter = 'all';
  let latestContext = null;
  const pageSize = 50;
  let visibleLimit = pageSize;
  let pageState = 'idle';

  function loadStyles() {
    if (typeof document === 'undefined') return;
    if (document.querySelector('link[data-fleet-view-style="work"]')) return;
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = '/ui/views/work.css';
    link.dataset.fleetViewStyle = 'work';
    document.head.append(link);
  }

  function useContext(context) {
    latestContext = context;
  }

  function groupFor(status) {
    return statusGroups.find(group => group.statuses.includes(status)) || {
      key: 'other',
      label: 'Other reported state',
      statuses: [],
    };
  }

  function groupLabel(key) {
    return statusGroups.find(group => group.key === key)?.label || 'Other reported state';
  }

  function nextAction(ticket) {
    const activity = activityRecord(ticket);
    if (activity) return activity.next_action;
    const status = ticket.status;
    if (ticket.delivery?.state === 'delivery_recorded') return 'Delivery recorded; inspect ticket evidence';
    if (ticket.delivery?.state === 'integration_merged') return 'Ready on the delivery branch; your team handles the final merge';
    if (ticket.delivery?.state === 'integration_pending') return 'Waiting for integration checks or collection to resume';
    if (ticket.delivery?.state === 'integration_blocked') return 'Resolve the integration blocker in ticket evidence';
    if (ticket.delivery?.state === 'pr_pending') return 'Butler is preparing the pull request';
    if (ticket.delivery?.state === 'pr_created') return 'Review the pull request';
    if (['pr_blocked', 'pr_uncertain'].includes(ticket.delivery?.state)) return 'Check delivery evidence in ticket details';
    if (ticket.dispatch_state?.state === 'parked') return 'Resolve the workflow blocker before resuming';
    if (status === 'needs_human') return 'A human decision is needed';
    if (status === 'rejected') return 'Address the reviewer feedback';
    if (['submitted', 'reviewing', 'in_review'].includes(status)) return 'Independent review is the next gate';
    if (['claimed', 'in_progress', 'creating_report'].includes(status)) return 'The owner prepares the required evidence';
    if (['open', 'offered', 'assigned'].includes(status)) return 'An eligible teammate must accept the work';
    if (status === 'closed') return 'Review approved; inspect the result';
    if (['canceled', 'terminated'].includes(status)) return 'No further work is scheduled';
    return 'Open the ticket for its recorded next step';
  }

  function activityRecord(ticket) {
    const value = ticket.activity;
    if (!value || value.schema_version !== 1 || typeof value !== 'object') return null;
    const stages = new Set(['intake', 'queued', 'work', 'validation', 'review', 'integration', 'delivery', 'completed']);
    const states = new Set(['waiting', 'running', 'blocked', 'retrying', 'failed', 'canceled', 'stale', 'unknown', 'completed']);
    if (!stages.has(value.stage) || !states.has(value.state) || typeof value.next_action !== 'string') return null;
    return value;
  }

  function activityLabel(value) {
    return value.replaceAll('_', ' ').replace(/(^|\s)\S/g, letter => letter.toUpperCase());
  }

  function ownerLabel(ticket) {
    if (ticket.claimed_by) return ticket.claimed_by;
    if (ticket.dispatch_state?.state === 'offered') return ticket.dispatch_state.agent_name || 'Offer pending';
    return 'Unassigned';
  }

  function durationLabel(milliseconds) {
    const seconds = Math.max(0, Math.ceil(milliseconds / 1000));
    if (seconds < 60) return `${seconds}s`;
    const minutes = Math.ceil(seconds / 60);
    if (minutes < 60) return `${minutes} min`;
    const hours = Math.floor(minutes / 60);
    const remainder = minutes % 60;
    return remainder ? `${hours} hr ${remainder} min` : `${hours} hr`;
  }

  function leaseLabel(ticket) {
    if (!['claimed', 'in_progress', 'creating_report'].includes(ticket.status)) return 'No active work lease';
    const expiresAt = Date.parse(ticket.lease_expires_at || '');
    if (!Number.isFinite(expiresAt)) return 'Lease time not supplied';
    const remaining = expiresAt - Date.now();
    if (remaining <= 0) return 'Lease expired';
    return `Lease · ${durationLabel(remaining)} remaining`;
  }

  function progressRecord(ticket) {
    if (!['claimed', 'in_progress', 'creating_report'].includes(ticket.status)) return null;
    const progress = ticket.progress;
    if (!progress || typeof progress !== 'object') return null;
    const {low_percent: low, high_percent: high, confidence} = progress;
    if (!Number.isInteger(low) || !Number.isInteger(high) || low < 0 || high > 99 || low > high) return null;
    if (!['low', 'medium', 'high'].includes(confidence)) return null;
    return {progress, low, high, confidence};
  }

  function progressEmptyLabel(ticket) {
    if (ticket.status === 'rejected') return 'Rework not assessed';
    if (['submitted', 'reviewing', 'in_review'].includes(ticket.status)) return 'Review state · no active estimate';
    if (ticket.status === 'closed') return 'Complete by workflow state';
    if (['canceled', 'terminated'].includes(ticket.status)) return 'Ended · no active estimate';
    return 'Progress not assessed';
  }

  function progressCell(ticket, context) {
    const {esc, fmt, relativeAge} = context;
    const record = progressRecord(ticket);
    if (!record) {
      return `<div class="work-progress-cell" data-progress-state="unknown">
        <span class="work-cell-label">Agent estimate</span>
        <strong>${esc(progressEmptyLabel(ticket))}</strong>
        <span>No percentage inferred from time or lease</span>
      </div>`;
    }
    const {progress, low, high, confidence} = record;
    const estimate = low === high ? `About ${low}%` : `${low}–${high}%`;
    const freshness = ['fresh', 'stale'].includes(ticket.progress_freshness)
      ? ticket.progress_freshness
      : 'unknown';
    const assessedAt = typeof progress.assessed_at === 'string' ? progress.assessed_at : '';
    const assessedLabel = assessedAt
      ? `assessed ${relativeAge(assessedAt)}`
      : 'assessment time not supplied';
    const freshnessLabel = freshness === 'stale'
      ? `Stale · ${assessedLabel}`
      : freshness === 'fresh'
      ? `Current · ${assessedLabel}`
      : `Freshness unknown · ${assessedLabel}`;
    const evidence = typeof progress.evidence === 'string' && progress.evidence.trim()
      ? progress.evidence.trim()
      : '';
    const rangeLabel = low === high
      ? `Agent-estimated progress ${low} percent`
      : `Agent-estimated progress between ${low} and ${high} percent`;
    return `<div class="work-progress-cell" data-progress-state="${esc(freshness)}">
      <span class="work-cell-label">Agent estimate</span>
      <strong>${esc(estimate)} · ${esc(confidence)} confidence</strong>
      <span class="work-progress-track" role="img" aria-label="${esc(rangeLabel)}">
        <span class="work-progress-range" style="--progress-low:${low};--progress-high:${high}"></span>
      </span>
      <span class="work-progress-freshness">${esc(freshnessLabel)}</span>
      ${assessedAt ? `<time class="sr-only" datetime="${esc(assessedAt)}">${esc(fmt(assessedAt))}</time>` : ''}
      ${evidence ? `<details class="work-progress-evidence"><summary>Assessment evidence</summary><p>${esc(evidence)}</p></details>` : ''}
    </div>`;
  }

  function activityCell(ticket, context) {
    const activity = activityRecord(ticket);
    if (!activity) return progressCell(ticket, context);
    const {esc, fmt, relativeAge} = context;
    const attempt = Number.isInteger(activity.attempt_id) && activity.attempt_id > 0
      ? `Attempt ${activity.attempt_id}`
      : 'Attempt unknown';
    const actor = typeof activity.actor_id === 'string' && activity.actor_id
      ? `Actor ${activity.actor_id}`
      : 'Actor unknown';
    const updatedAt = typeof activity.updated_at === 'string' ? activity.updated_at : '';
    const freshness = ['fresh', 'stale', 'unknown'].includes(activity.freshness)
      ? activity.freshness
      : 'unknown';
    const freshnessLabel = freshness === 'fresh'
      ? 'Current'
      : freshness === 'stale'
      ? 'Stale'
      : 'Freshness unknown';
    const updatedLabel = updatedAt ? `updated ${relativeAge(updatedAt)}` : 'update time unknown';
    const estimate = activity.estimate;
    let estimateMarkup = '';
    if (estimate && Number.isInteger(estimate.low_percent) && Number.isInteger(estimate.high_percent)
        && estimate.low_percent >= 0 && estimate.high_percent <= 99
        && estimate.low_percent <= estimate.high_percent
        && ['low', 'medium', 'high'].includes(estimate.confidence)) {
      const label = estimate.low_percent === estimate.high_percent
        ? `About ${estimate.low_percent}%`
        : `${estimate.low_percent}–${estimate.high_percent}%`;
      const rangeLabel = estimate.low_percent === estimate.high_percent
        ? `Agent-estimated progress ${estimate.low_percent} percent`
        : `Agent-estimated progress between ${estimate.low_percent} and ${estimate.high_percent} percent`;
      estimateMarkup = `<span class="work-activity-estimate">${esc(label)} · ${esc(estimate.confidence)} confidence</span>
        <span class="work-progress-track" role="img" aria-label="${esc(rangeLabel)}">
          <span class="work-progress-range" style="--progress-low:${estimate.low_percent};--progress-high:${estimate.high_percent}"></span>
        </span>`;
    }
    const blocker = typeof activity.blocking_reason === 'string' && activity.blocking_reason.trim()
      ? `<p class="work-activity-blocker"><strong>Blocked:</strong> ${esc(activity.blocking_reason.trim())}</p>`
      : '';
    const refs = Array.isArray(activity.evidence_refs)
      ? activity.evidence_refs.filter(ref => typeof ref === 'string').slice(0, 8)
      : [];
    const evidenceMarkup = refs.length
      ? `<details class="work-activity-evidence"><summary>Evidence references (${refs.length})</summary>
          <ul>${refs.map(ref => `<li><code>${esc(ref)}</code></li>`).join('')}</ul>
        </details>`
      : '<span>No evidence reference supplied</span>';
    return `<div class="work-progress-cell work-activity-cell" data-progress-state="${esc(freshness)}" data-activity-state="${esc(activity.state)}">
      <span class="work-cell-label">Lifecycle activity</span>
      <strong>${esc(activityLabel(activity.stage))} · ${esc(activityLabel(activity.state))}</strong>
      <span>${esc(attempt)} · ${esc(actor)}</span>
      <span>${esc(freshnessLabel)} · ${esc(updatedLabel)}</span>
      ${updatedAt ? `<time class="sr-only" datetime="${esc(updatedAt)}">${esc(fmt(updatedAt))}</time>` : ''}
      <span>Boundary · ${esc(activity.completion_boundary || 'unknown')}</span>
      ${estimateMarkup}
      ${blocker}
      ${evidenceMarkup}
    </div>`;
  }

  function statusLabel(ticket) {
    return ticket.status_label || groupFor(ticket.status).label;
  }

  function ticketRow(item, context) {
    const {esc, fmt, ticketHref, boardHref} = context;
    const {central, board, ticket} = item;
    const detailHref = ticketHref(central, board.board_id, ticket.id);
    const group = groupFor(ticket.status);
    const skills = Array.isArray(ticket.skills_required) ? ticket.skills_required : [];
    return `<article class="work-ledger-row" data-pursers-ticket="${esc(ticket.id)}" data-work-state="${esc(group.key)}">
      <div class="work-state-cell">
        <span class="work-state-mark" aria-hidden="true"></span>
        <span class="work-state-label">${esc(statusLabel(ticket))}</span>
        <span class="work-updated">Updated ${esc(fmt(ticket.updated_at))}</span>
      </div>
      <div class="work-ticket-cell">
        <a class="work-ticket-title" href="${detailHref}">${esc(ticket.title || '(untitled)')}</a>
        <span class="work-ticket-id">${esc(ticket.id)}</span>
        <span class="work-project">${esc(board.label)} · ${esc(central)}</span>
      </div>
      ${activityCell(ticket, context)}
      <div class="work-owner-cell">
        <span class="work-cell-label">Owner</span>
        <strong>${esc(ownerLabel(ticket))}</strong>
        <span>${esc(leaseLabel(ticket))}</span>
      </div>
      <div class="work-next-cell">
        <span class="work-cell-label">Next</span>
        <strong>${esc(nextAction(ticket))}</strong>
        <span>Tier ${esc(ticket.tier || 2)}${skills.length ? ` · ${esc(skills.length)} required skill${skills.length === 1 ? '' : 's'}` : ''}</span>
      </div>
      <div class="work-actions-cell">
        <a class="primary-action" href="${detailHref}">Open details</a>
        ${ticket.delivery?.url && /^https:\/\//i.test(ticket.delivery.url) ? `<a href="${esc(ticket.delivery.url)}" target="_blank" rel="noopener noreferrer">Open PR${ticket.delivery.pr_id ? ` #${esc(ticket.delivery.pr_id)}` : ''}</a>` : ''}
        <details class="work-evidence">
          <summary>Evidence and flow</summary>
          <nav aria-label="Evidence for ${esc(ticket.id)}">
            <a href="${boardHref(central, board.board_id, 'timeline')}">Timeline</a>
            <a href="${boardHref(central, board.board_id, 'changes')}">Changes</a>
            <a href="${boardHref(central, board.board_id, 'flow')}">Flow</a>
            <a href="${boardHref(central, board.board_id, 'routes')}">Routes</a>
          </nav>
        </details>
      </div>
    </article>`;
  }

  function filterButton(key, label, count) {
    const active = activeFilter === key;
    return `<button type="button" class="work-filter" data-work-filter="${key}" aria-pressed="${active}">
      <span>${label}</span><strong>${count}</strong>
    </button>`;
  }

  function kanbanCard(item, context) {
    const {esc, fmt, ticketHref} = context;
    const {central, board, ticket} = item;
    const href = ticketHref(central, board.board_id, ticket.id);
    const activity = activityRecord(ticket);
    const activityText = activity
      ? `${activityLabel(activity.stage)} · ${activityLabel(activity.state)}`
      : progressEmptyLabel(ticket);
    return `<article class="work-kanban-card" data-pursers-ticket="${esc(ticket.id)}" data-work-state="${esc(groupFor(ticket.status).key)}">
      <div class="work-kanban-card-head"><span class="work-state-mark" aria-hidden="true"></span><span>${esc(statusLabel(ticket))}</span></div>
      <a class="work-ticket-title" href="${href}">${esc(ticket.title || '(untitled)')}</a>
      <code>${esc(ticket.id)}</code>
      <p>${esc(activityText)}</p>
      <dl><div><dt>Owner</dt><dd>${esc(ownerLabel(ticket))}</dd></div><div><dt>Next</dt><dd>${esc(nextAction(ticket))}</dd></div></dl>
      <footer><span>${esc(board.label)} · ${esc(fmt(ticket.updated_at))}</span><a href="${href}">Open details</a></footer>
    </article>`;
  }

  function renderKanban(visible, context) {
    const {esc} = context;
    const populated = statusGroups.filter(group => visible.some(item => group.statuses.includes(item.ticket.status)));
    const other = visible.some(item => !knownStatuses.has(item.ticket.status))
      ? [{key: 'other', label: 'Other reported state', statuses: []}]
      : [];
    return `<section class="work-kanban" aria-labelledby="work-ledger-title" data-work-layout="kanban">
      ${[...populated, ...other].map(group => {
        const items = visible.filter(item => group.key === 'other'
          ? !knownStatuses.has(item.ticket.status)
          : group.statuses.includes(item.ticket.status));
        return `<section class="work-kanban-lane" data-work-lane="${esc(group.key)}">
          <header><h4>${esc(group.label)}</h4><span>${esc(items.length)}</span></header>
          <div>${items.map(item => kanbanCard(item, context)).join('')}</div>
        </section>`;
      }).join('')}
    </section>`;
  }

  function renderMobileList(visible, context) {
    return `<section class="work-mobile-list" aria-labelledby="work-ledger-title" data-work-layout="list">
      ${visible.map(item => ticketRow(item, context)).join('')}
    </section>`;
  }

  function renderWarmWork() {
    const context = latestContext;
    const {esc, pageHead, warmTruthStrip, warmTickets} = context;
    const seen = new Set();
    const tickets = warmTickets().filter(item => {
      const key = JSON.stringify([item.central, item.board.board_id, item.ticket.id]);
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
    const counts = Object.fromEntries(statusGroups.map(group => [
      group.key,
      tickets.filter(item => group.statuses.includes(item.ticket.status)).length,
    ]));
    counts.other = tickets.filter(item => !knownStatuses.has(item.ticket.status)).length;
    const filters = [
      filterButton('all', 'All visible', tickets.length),
      ...statusGroups
        .filter(group => counts[group.key] > 0)
        .map(group => filterButton(group.key, group.label, counts[group.key])),
      ...(counts.other ? [filterButton('other', 'Other', counts.other)] : []),
    ].join('');
    const matching = activeFilter === 'all'
      ? tickets
      : tickets.filter(item => groupFor(item.ticket.status).key === activeFilter);
    const visible = matching.slice(0, visibleLimit);
    const hasMore = visible.length < matching.length;
    const activeLabel = activeFilter === 'all' ? 'All visible' : groupLabel(activeFilter);
    const filterOpen = typeof matchMedia === 'function' && matchMedia('(min-width: 801px)').matches ? ' open' : '';
    const ledger = visible.length
      ? `${renderKanban(visible, context)}${renderMobileList(visible, context)}`
      : `<div class="empty-guidance work-empty">
          <h3>No work in this view</h3>
          <p>${tickets.length ? 'Choose another state to see its visible work.' : 'Choose a project and use its guarded intake to describe the result you need. Older work may exist outside this bounded view.'}</p>
          <div class="card-actions">${tickets.length ? '<button type="button" class="button" data-work-filter="all">Show all visible work</button>' : '<a class="primary-action" href="#/projects">Choose a project</a>'}</div>
        </div>`;
    return `<div class="work-view">
      ${pageHead('Work', 'Work across every project', 'Scan state, ownership, lease evidence, and the next recorded gate without opening every ticket.')}
      ${warmTruthStrip()}
      <section class="work-filter-panel" aria-labelledby="work-filter-title">
        <div>
          <h3 id="work-filter-title">Visible work</h3>
          <p class="muted">Filters persist while live data refreshes. Counts reflect this bounded snapshot.</p>
        </div>
        <details class="work-filter-disclosure"${filterOpen}>
          <summary>Filters: ${activeLabel} (${visible.length})</summary>
          <div class="work-filters" role="group" aria-label="Filter work by state">${filters}</div>
        </details>
      </section>
      <h3 id="work-ledger-title" class="sr-only">${activeFilter === 'all' ? 'All visible work' : `${groupLabel(activeFilter)} work`}</h3>
      <p class="work-result-count" aria-live="polite">Showing ${visible.length} of ${matching.length} matching ticket${matching.length === 1 ? '' : 's'} (${tickets.length} loaded)</p>
      ${ledger}
      <div class="work-page-controls" data-page-state="${esc(pageState)}">
        ${hasMore ? `<button type="button" class="button" data-work-next-page ${pageState === 'loading' ? 'disabled' : ''}>${pageState === 'loading' ? 'Loading…' : `Show next ${Math.min(pageSize, matching.length - visible.length)}`}</button>` : '<span class="muted">End of loaded tickets</span>'}
      </div>
    </div>`;
  }

  document.addEventListener('click', event => {
    const button = event.target.closest('[data-work-filter]');
    if (!button || !latestContext || !document.querySelector('.work-view')) return;
    activeFilter = button.dataset.workFilter;
    visibleLimit = pageSize;
    pageState = 'idle';
    const host = document.querySelector('#central-sections');
    if (host) host.innerHTML = renderWarmWork();
  });

  document.addEventListener('click', event => {
    const button = event.target.closest('[data-work-next-page]');
    if (!button || !latestContext || !document.querySelector('.work-view')) return;
    pageState = 'loading';
    button.disabled = true;
    button.textContent = 'Loading…';
    visibleLimit += pageSize;
    pageState = 'idle';
    const host = document.querySelector('#central-sections');
    if (host) host.innerHTML = renderWarmWork();
  });

  loadStyles();
  globalThis.FleetViewModules.register({
    id: 'work',
    owns: ['ticket work queue', 'ticket filters', 'work empty states'],
    render(context) {
      useContext(context);
      return renderWarmWork();
    },
  });
})();
