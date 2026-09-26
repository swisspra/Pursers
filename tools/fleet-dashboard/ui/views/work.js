/* Fleet route module: work. */
(function registerWorkView() {
  'use strict';

  let esc, fmt, pageHead, warmTruthStrip, warmTickets, ticketHref, boardHref;

  const STYLE_URL = '/ui/views/work.css';
  const GROUPS = [
    {id: 'ready', title: 'Ready to start', copy: 'Unclaimed work waiting for an eligible worker.', states: ['open', 'assigned']},
    {id: 'working', title: 'In progress', copy: 'A worker owns the next evidence-producing step.', states: ['claimed', 'in_progress', 'creating_report']},
    {id: 'review', title: 'Review handoff', copy: 'Submitted evidence is waiting for or undergoing independent review.', states: ['submitted', 'reviewing', 'in_review']},
    {id: 'attention', title: 'Needs attention', copy: 'A human answer or review change is required before completion.', states: ['needs_human', 'rejected']},
    {id: 'done', title: 'Completed', copy: 'Review approved the recorded result.', states: ['closed']},
    {id: 'ended', title: 'Ended', copy: 'Canceled and terminated work remains visible.', states: ['canceled', 'terminated']},
  ];
  const GROUP_BY_STATE = new Map(GROUPS.flatMap(group => group.states.map(state => [state, group])));

  function loadStyles() {
    if (typeof document === 'undefined') return;
    if (document.querySelector('link[data-fleet-view-style="work"]')) return;
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = STYLE_URL;
    link.dataset.fleetViewStyle = 'work';
    document.head.append(link);
  }

  function useContext(context) {
    ({esc, fmt, pageHead, warmTruthStrip, warmTickets, ticketHref, boardHref} = context);
  }

  function duration(seconds) {
    const value = Number(seconds);
    if (!Number.isFinite(value) || value < 0) return null;
    if (value < 60) return `${Math.floor(value)}s`;
    if (value < 3600) return `${Math.floor(value / 60)}m`;
    if (value < 86400) return `${Math.floor(value / 3600)}h`;
    return `${Math.floor(value / 86400)}d`;
  }

  function ownerLabel(ticket) {
    const state = String(ticket.status || 'unknown');
    if (['claimed', 'in_progress', 'creating_report'].includes(state)) {
      return ticket.claimed_by || 'Worker not supplied';
    }
    const review = String(ticket.status_label || '').match(/^in review by (.+)$/i);
    if (review) return review[1];
    if (['submitted', 'reviewing', 'in_review'].includes(state)) return 'Independent reviewer pending';
    if (state === 'open') return 'Unclaimed';
    return ticket.claimed_by || 'No active owner';
  }

  function roleLabel(ticket) {
    const state = String(ticket.status || 'unknown');
    if (['submitted', 'reviewing', 'in_review'].includes(state)) return 'Reviewer';
    if (['claimed', 'in_progress', 'creating_report'].includes(state)) return 'Worker';
    return 'Owner';
  }

  function leaseLabel(ticket) {
    const state = String(ticket.status || 'unknown');
    if (['claimed', 'in_progress', 'creating_report', 'reviewing', 'in_review'].includes(state)) {
      const age = duration(ticket.claim_age_s);
      return age ? `Active · claimed ${age} ago` : 'Active · expiry in ticket detail';
    }
    if (state === 'submitted') return 'Reviewer lease pending';
    return 'No active lease';
  }

  function nextAction(ticket) {
    return {
      open: 'An eligible worker can claim this ticket.',
      assigned: 'The assigned worker can accept the ticket.',
      claimed: 'Worker completes evidence and submits for review.',
      in_progress: 'Worker completes evidence and submits for review.',
      creating_report: 'Worker finishes the report and submits for review.',
      submitted: 'An independent reviewer checks the submitted evidence.',
      reviewing: 'Reviewer records approval or requests changes.',
      in_review: 'Reviewer records approval or requests changes.',
      needs_human: 'A person must answer before work can continue.',
      rejected: 'Worker addresses the review and resubmits.',
      closed: 'No action needed.',
      canceled: 'No action available.',
      terminated: 'No action available.',
    }[ticket.status] || 'Open the ticket for the recorded next step.';
  }

  function ticketCard({central, board, ticket}) {
    const status = String(ticket.status || 'unknown');
    const statusLabel = ticket.status_label || status;
    const updated = ticket.updated_at ? fmt(ticket.updated_at) : 'Not supplied in bounded list';
    const abandoned = Number(ticket.abandoned_count || 0);
    const warnings = [
      abandoned > 0 ? `${abandoned} lease lapse${abandoned === 1 ? '' : 's'} recorded` : '',
      ticket.lease_renewal_source === 'keepalive' ? 'Keepalive renewal source' : '',
    ].filter(Boolean);

    return `<article class="work-ticket" data-work-ticket data-ticket-status="${esc(status)}">
      <header class="work-ticket-head">
        <div class="work-ticket-route">
          <a class="id" href="${ticketHref(central, board.board_id, ticket.id)}">${esc(ticket.id)}</a>
          <span>${esc(central)}</span><span aria-hidden="true">/</span><span>${esc(board.label)}</span>
        </div>
        <span class="status work-status">${esc(statusLabel)}</span>
      </header>
      <h4><a href="${ticketHref(central, board.board_id, ticket.id)}">${esc(ticket.title || '(untitled)')}</a></h4>
      <dl class="work-facts">
        <div><dt>${esc(roleLabel(ticket))}</dt><dd>${esc(ownerLabel(ticket))}</dd></div>
        <div><dt>Lease</dt><dd>${esc(leaseLabel(ticket))}</dd></div>
        <div><dt>Evidence</dt><dd>Updated ${esc(updated)}</dd></div>
      </dl>
      ${warnings.length ? `<p class="work-warning warning">${warnings.map(esc).join(' · ')}</p>` : ''}
      <div class="work-next"><span>Next</span><p>${esc(nextAction(ticket))}</p></div>
      <footer class="work-ticket-actions">
        <a class="primary-action" href="${ticketHref(central, board.board_id, ticket.id)}">Open ticket</a>
        <a class="button" href="${boardHref(central, board.board_id, 'timeline')}">Timeline</a>
        <a class="button" href="${boardHref(central, board.board_id, 'changes')}">Changes</a>
      </footer>
    </article>`;
  }

  function workGroup(group, items) {
    return `<section class="work-group" data-work-group="${esc(group.id)}">
      <header class="work-group-head">
        <div><h3>${esc(group.title)}</h3><p>${esc(group.copy)}</p></div>
        <span class="status">${esc(items.length)}</span>
      </header>
      <div class="work-ticket-list">${items.map(ticketCard).join('')}</div>
    </section>`;
  }

  function renderWarmWork() {
    const tickets = warmTickets();
    const grouped = new Map(GROUPS.map(group => [group.id, []]));
    const unknown = [];
    for (const item of tickets) {
      const group = GROUP_BY_STATE.get(item.ticket.status);
      if (group) grouped.get(group.id).push(item);
      else unknown.push(item);
    }
    const count = states => tickets.filter(item => states.includes(item.ticket.status)).length;
    const summary = tickets.length ? `<section class="work-summary" aria-label="Work summary">
      <div><span>Ready</span><b>${esc(count(['open', 'assigned']))}</b></div>
      <div><span>Working</span><b>${esc(count(['claimed', 'in_progress', 'creating_report']))}</b></div>
      <div><span>Review ready</span><b>${esc(count(['submitted', 'reviewing', 'in_review']))}</b></div>
      <div><span>Needs attention</span><b>${esc(count(['needs_human', 'rejected']))}</b></div>
    </section>` : '';
    const sections = GROUPS
      .map(group => [group, grouped.get(group.id)])
      .filter(([, items]) => items.length)
      .map(([group, items]) => workGroup(group, items));
    if (unknown.length) {
      sections.push(workGroup({id: 'unknown', title: 'Other states', copy: 'Unknown states remain visible and unchanged.'}, unknown));
    }
    const empty = `<section class="empty-guidance work-empty"><h3>No work yet</h3><p>Choose a project and use its guarded intake to describe the outcome you need.</p><div class="card-actions"><a class="primary-action" href="#/projects">Choose a project</a></div></section>`;

    return `${pageHead('Work', 'Work in motion', 'See who owns each ticket, the recorded handoff, lease context, evidence recency, and the next protocol step.')}${warmTruthStrip()}${summary}<div class="work-board">${sections.join('') || empty}</div>`;
  }

  loadStyles();
  globalThis.FleetViewModules.register({
    id: 'work',
    owns: [
      'ticket work queue',
      'ticket lifecycle and handoff presentation',
      'work empty states',
    ],
    render(context) { useContext(context); return renderWarmWork(); },
  });
})();
