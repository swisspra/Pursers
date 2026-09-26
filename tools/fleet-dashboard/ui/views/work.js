/* Fleet route module: work. */
(function registerWorkView() {
  'use strict';

  const statusGroups = [
    {key: 'attention', label: 'Needs attention', statuses: ['needs_human', 'rejected']},
    {key: 'review', label: 'Being reviewed', statuses: ['submitted', 'reviewing', 'in_review']},
    {key: 'working', label: 'In progress', statuses: ['claimed', 'in_progress', 'creating_report']},
    {key: 'finding', label: 'Finding a teammate', statuses: ['open', 'offered', 'assigned']},
    {key: 'complete', label: 'Reviewed and complete', statuses: ['closed']},
    {key: 'ended', label: 'Ended', statuses: ['canceled', 'terminated']},
  ];
  const knownStatuses = new Set(statusGroups.flatMap(group => group.statuses));
  let activeFilter = 'all';
  let latestContext = null;

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

  function nextAction(status) {
    if (status === 'needs_human') return 'A human decision is needed';
    if (status === 'rejected') return 'Address the reviewer feedback';
    if (['submitted', 'reviewing', 'in_review'].includes(status)) return 'Independent review is the next gate';
    if (['claimed', 'in_progress', 'creating_report'].includes(status)) return 'The owner prepares the required evidence';
    if (['open', 'offered', 'assigned'].includes(status)) return 'An eligible teammate must accept the work';
    if (status === 'closed') return 'Review approved; inspect the result';
    if (['canceled', 'terminated'].includes(status)) return 'No further work is scheduled';
    return 'Open the ticket for its recorded next step';
  }

  function ownerLabel(ticket) {
    if (ticket.claimed_by) return ticket.claimed_by;
    if (ticket.dispatch_state?.state === 'offered') return ticket.dispatch_state.agent_name || 'Offer pending';
    return 'Unassigned';
  }

  function leaseLabel(ticket) {
    if (!['claimed', 'in_progress', 'creating_report'].includes(ticket.status)) return 'No active work lease';
    if (ticket.ttl_s === null || ticket.ttl_s === undefined) return 'Lease timing not supplied';
    return `Lease window ${ticket.ttl_s}s`;
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
      <div class="work-owner-cell">
        <span class="work-cell-label">Owner</span>
        <strong>${esc(ownerLabel(ticket))}</strong>
        <span>${esc(leaseLabel(ticket))}</span>
      </div>
      <div class="work-next-cell">
        <span class="work-cell-label">Next</span>
        <strong>${esc(nextAction(ticket.status))}</strong>
        <span>Tier ${esc(ticket.tier || 2)}${skills.length ? ` · ${esc(skills.length)} required skill${skills.length === 1 ? '' : 's'}` : ''}</span>
      </div>
      <div class="work-actions-cell">
        <a class="primary-action" href="${detailHref}">Open details</a>
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

  function renderWarmWork() {
    const context = latestContext;
    const {pageHead, warmTruthStrip, warmTickets} = context;
    const tickets = warmTickets();
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
    const visible = activeFilter === 'all'
      ? tickets
      : tickets.filter(item => groupFor(item.ticket.status).key === activeFilter);
    const activeLabel = activeFilter === 'all' ? 'All visible' : groupLabel(activeFilter);
    const filterOpen = typeof matchMedia === 'function' && matchMedia('(min-width: 801px)').matches ? ' open' : '';
    const ledger = visible.length
      ? `<section class="work-ledger" aria-labelledby="work-ledger-title">
          <div class="work-ledger-head" aria-hidden="true">
            <span>State</span><span>Work item</span><span>Owner and lease</span><span>Next action</span><span>Open</span>
          </div>
          <div class="work-ledger-body">${visible.map(item => ticketRow(item, context)).join('')}</div>
        </section>`
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
      <p class="work-result-count" aria-live="polite">Showing ${visible.length} of ${tickets.length} visible ticket${tickets.length === 1 ? '' : 's'}</p>
      ${ledger}
    </div>`;
  }

  document.addEventListener('click', event => {
    const button = event.target.closest('[data-work-filter]');
    if (!button || !latestContext || !document.querySelector('.work-view')) return;
    activeFilter = button.dataset.workFilter;
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
