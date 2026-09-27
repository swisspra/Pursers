/* Fleet route module: activity. */
(function registerActivityView() {
  'use strict';

  let esc, fmt, pageHead, warmTruthStrip, warmTickets, ticketHref, boardHref,
    warmBoards, autonomousRows, autonomousStateLabel, fleetData;

  function useContext(context) {
    ({esc, fmt, pageHead, warmTruthStrip, warmTickets, ticketHref, boardHref,
      warmBoards, autonomousRows, autonomousStateLabel} = context);
    fleetData = context.fleetData;
  }

  function loadActivityStyles() {
    if (document.querySelector('link[data-fleet-view-style="activity"]')) return;
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = '/ui/views/activity.css';
    link.dataset.fleetViewStyle = 'activity';
    document.head.append(link);
  }

  function activityText(value, fallback = 'Not observed') {
    return value === null || value === undefined || value === '' ? fallback : value;
  }

  function activityLabel(value) {
    return String(activityText(value)).replaceAll('_', ' ');
  }

  function activityDay(value) {
    if (!value) return 'Time not observed';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return 'Time not observed';
    return new Intl.DateTimeFormat(undefined, {
      weekday: 'long', month: 'short', day: 'numeric'
    }).format(date);
  }

  function activityOutcome(event) {
    if (event.status_from || event.status_to) {
      return `${activityLabel(event.status_from)} → ${activityLabel(event.status_to)}`;
    }
    if (event.review_verdict) return activityLabel(event.review_verdict);
    return activityLabel(event.kind);
  }

  function activityRows() {
    const tickets = new Map(
      warmTickets().map(({central, board, ticket}) => [
        `${central}\0${board.board_id}\0${ticket.id}`,
        ticket
      ])
    );
    const rows = [];
    for (const {central, board} of warmBoards()) {
      for (const event of board.events || []) {
        rows.push({
          central,
          board,
          event,
          ticket: event.ticket_id
            ? tickets.get(`${central}\0${board.board_id}\0${event.ticket_id}`)
            : null
        });
      }
    }
    return rows.sort((left, right) => {
      const leftTime = Date.parse(left.event.occurred_at || '') || 0;
      const rightTime = Date.parse(right.event.occurred_at || '') || 0;
      return rightTime - leftTime ||
        (Number.isInteger(right.event.seq) ? right.event.seq : -1) -
        (Number.isInteger(left.event.seq) ? left.event.seq : -1);
    });
  }

  function renderActivityEvent({central, board, event, ticket}) {
    const ticketId = activityText(event.ticket_id);
    const actor = activityText(event.actor);
    const role = activityText(event.actor_role || event.role);
    const sequence = Number.isInteger(event.seq) ? `#${event.seq}` : 'Sequence not observed';
    const ticketLink = event.ticket_id
      ? `<a class="activity-ticket id" href="${ticketHref(central, board.board_id, event.ticket_id)}">${esc(ticketId)}</a>`
      : `<span class="activity-ticket id">${esc(ticketId)}</span>`;
    const title = ticket?.title
      ? `<span class="activity-title">${esc(ticket.title)}</span>`
      : '<span class="activity-title muted">Ticket title not present in this bounded view</span>';
    const time = event.occurred_at
      ? `<time datetime="${esc(event.occurred_at)}">${esc(fmt(event.occurred_at))}</time>`
      : '<span>Not observed</span>';

    return `<li class="activity-event" data-event-seq="${esc(activityText(event.seq))}" data-pursers-state="ready">
      <div class="activity-marker" aria-hidden="true"></div>
      <div class="activity-main">
        <div class="activity-outcome-row">
          <span class="activity-outcome">${esc(activityOutcome(event))}</span>
          <span class="activity-time">${time}</span>
        </div>
        <div class="activity-ticket-row">${ticketLink}${title}</div>
        <dl class="activity-provenance" aria-label="Event provenance">
          <div><dt>Board</dt><dd>${esc(board.label)}<span>${esc(board.board_id)}</span></dd></div>
          <div><dt>Actor</dt><dd>${esc(actor)}</dd></div>
          <div><dt>Role</dt><dd>${esc(role)}</dd></div>
          <div><dt>Source</dt><dd>Board journal<span>${esc(sequence)} · ${esc(central)}</span></dd></div>
        </dl>
        <div class="activity-actions">
          ${event.ticket_id ? `<a href="${ticketHref(central, board.board_id, event.ticket_id)}">Ticket detail</a>` : ''}
          <a href="${boardHref(central, board.board_id, 'timeline')}">Board timeline</a>
          <a href="${boardHref(central, board.board_id, 'changes')}">Changes</a>
          <a href="${boardHref(central, board.board_id, 'routes')}">Routes</a>
        </div>
      </div>
    </li>`;
  }

  function renderActivityTimeline(rows) {
    if (!rows.length) {
      return `<section class="activity-empty" data-pursers-panel="activity" data-pursers-state="empty">
        <div class="activity-empty-mark" aria-hidden="true">○</div>
        <div><h3>No activity observed</h3><p>The connected board snapshots contain no retained journal events. Open a board timeline to inspect its current bounded history.</p></div>
      </section>`;
    }

    const groups = [];
    for (const row of rows) {
      const day = activityDay(row.event.occurred_at);
      let group = groups.at(-1);
      if (!group || group.day !== day) {
        group = {day, rows: []};
        groups.push(group);
      }
      group.rows.push(row);
    }

    return `<section class="activity-ledger" data-pursers-panel="activity" data-pursers-state="ready" aria-label="Source-backed activity timeline">
      ${groups.map(group => `<section class="activity-day"><h3>${esc(group.day)}</h3><ol>${group.rows.map(renderActivityEvent).join('')}</ol></section>`).join('')}
    </section>`;
  }

  function caseStudyMetric(metric, suffix = '') {
    if (!metric || metric.status !== 'available' || !Number.isFinite(metric.seconds)) {
      return 'Unavailable';
    }
    const seconds = metric.seconds;
    const value = seconds >= 3600
      ? `${(seconds / 3600).toFixed(1)} h`
      : seconds >= 60
        ? `${(seconds / 60).toFixed(1)} min`
        : `${seconds.toFixed(1)} s`;
    return `${value}${suffix}`;
  }

  function caseStudyRun(study, run, central) {
    const ticketRows = (run.tickets || []).map(ticket => {
      const phases = (ticket.timeline || []).map(item => `<li data-phase="${esc(item.phase)}">
        <span>${esc(activityLabel(item.phase))}</span>
        <time datetime="${esc(item.occurred_at)}">${esc(fmt(item.occurred_at))}</time>
        <small>${esc(activityLabel(item.quality))} · ${esc(activityLabel(item.source))}</small>
      </li>`).join('');
      return `<details class="case-study-ticket" data-pursers-ticket="${esc(ticket.ticket_id)}">
        <summary><a class="id" href="${ticketHref(central, ticket.board_id, ticket.ticket_id)}">${esc(ticket.ticket_id)}</a><span>${ticket.accepted ? 'Accepted' : 'Incomplete'}</span></summary>
        <ol class="case-study-phases">${phases || '<li><span>Lifecycle unavailable</span></li>'}</ol>
        <dl class="case-study-ticket-metrics">
          <div><dt>Queue</dt><dd>${esc(caseStudyMetric(ticket.metrics?.queue_time))}</dd></div>
          <div><dt>Ownership</dt><dd>${esc(caseStudyMetric(ticket.metrics?.ownership_time))}</dd></div>
          <div><dt>Review queue</dt><dd>${esc(caseStudyMetric(ticket.metrics?.review_queue_time))}</dd></div>
          <div><dt>Review work</dt><dd>${esc(caseStudyMetric(ticket.metrics?.review_work_time))}</dd></div>
        </dl>
      </details>`;
    }).join('');
    return `<section class="case-study-lane" data-arm="${esc(run.arm)}" data-state="${run.complete ? 'complete' : 'incomplete'}">
      <div class="case-study-lane-head"><div><p class="eyebrow">${esc(activityLabel(run.arm))}</p><h4>${esc(run.run_id)}</h4></div><span class="status">${run.complete ? 'Complete' : 'Evidence incomplete'}</span></div>
      <dl class="case-study-run-metrics">
        <div><dt>Wall time</dt><dd>${esc(caseStudyMetric(run.wall_time))}</dd></div>
        <div><dt>Observed peak</dt><dd>${esc(run.observed_peak_concurrency)} concurrent</dd></div>
        <div><dt>Accepted</dt><dd>${esc(run.accepted_count)} / ${esc((run.tickets || []).length)}</dd></div>
        <div><dt>Cost</dt><dd>Unavailable</dd></div>
      </dl>
      ${run.missing?.length ? `<p class="warning">${esc(run.missing.join(' · '))}</p>` : ''}
      <div class="case-study-tickets">${ticketRows}</div>
    </section>`;
  }

  function renderCaseStudies() {
    const rows = [];
    for (const [central, data] of Object.entries(fleetData || {})) {
      for (const study of data.case_studies || []) rows.push({central, study});
    }
    if (!rows.length) return '';
    return `<section class="case-studies" aria-labelledby="case-study-title">
      <div class="case-study-heading"><div><p class="eyebrow">Measured runs</p><h3 id="case-study-title">Sequential and parallel evidence</h3></div><p>Only preregistered, source-backed pairs produce speedup. Missing or mismatched evidence stays visible.</p></div>
      ${rows.map(({central, study}) => {
        const runs = new Map((study.runs || []).map(run => [run.run_id, run]));
        return (study.comparisons || []).map(comparison => {
          const sequential = runs.get(comparison.sequential_run_id);
          const parallel = runs.get(comparison.parallel_run_id);
          const ratio = comparison.speedup?.status === 'available' ? `${comparison.speedup.ratio.toFixed(2)}×` : 'Unavailable';
          return `<article class="case-study" data-comparison-state="${esc(comparison.status)}">
            <header><div><p class="eyebrow">${esc(study.study_id)} · ${esc(comparison.pair_id)}</p><h3>${esc(ratio)} speedup</h3></div><span class="status">${esc(activityLabel(comparison.status))}</span></header>
            ${comparison.reasons?.length ? `<p class="warning">${esc(comparison.reasons.join(' '))}</p>` : '<p class="meta">Matched ticket templates and complete source windows.</p>'}
            <div class="case-study-lanes">${sequential ? caseStudyRun(study, sequential, central) : ''}${parallel ? caseStudyRun(study, parallel, central) : ''}</div>
            <p class="meta">Cost unavailable unless supplied by source telemetry. No estimate is inferred from model, time, or token fields.</p>
          </article>`;
        }).join('');
      }).join('')}
    </section>`;
  }

  function renderWarmActivity() {
    const allRows = activityRows();
    const rows = allRows.slice(0, 20);
    const boards = warmBoards();
    const activeBoards = new Set(allRows.map(row => `${row.central}\0${row.board.board_id}`)).size;
    const omitted = Math.max(0, allRows.length - rows.length);
    const boundary = omitted
      ? `${omitted} older retained event${omitted === 1 ? '' : 's'} not shown.`
      : 'Older events may exist outside these bounded snapshots.';

    return `${pageHead('Activity','What changed, with its source','A chronological board-journal view. Missing actor, role, or outcome detail is labeled instead of inferred.')}
      ${warmTruthStrip()}
      <section class="activity-scope" aria-label="Activity coverage">
        <div><b>${esc(rows.length)}</b><span>newest events shown</span></div>
        <div><b>${esc(activeBoards)} / ${esc(boards.length)}</b><span>boards with retained events</span></div>
        <p><b>Bounded history.</b> ${esc(boundary)}</p>
      </section>
      ${renderCaseStudies()}
      ${renderActivityTimeline(rows)}`;
  }

  function renderAutonomousActivity() {
    const cards = autonomousRows().map(({central, board, data, error}) => {
      if (error) {
        return `<article class="card autonomous-activity-card" data-pursers-state="error">
          <p class="eyebrow">${esc(board.label)} · ${esc(central)}</p>
          <h3>Butler audit unavailable</h3>
          <p class="error autonomous-state-note">${esc(error)}</p>
        </article>`;
      }
      if (!data) {
        return `<article class="card autonomous-activity-card" data-pursers-state="loading" aria-busy="true">
          <p class="eyebrow">${esc(board.label)} · ${esc(central)}</p>
          <h3>Loading Butler audit…</h3>
          <p class="muted autonomous-state-note">Waiting for this board's bounded command history.</p>
        </article>`;
      }
      return `<article class="card autonomous-activity-card" data-pursers-state="ready">
      <div class="section-title"><div><p class="eyebrow">${esc(board.label)} · ${esc(central)}</p><h3>Butler command audit</h3></div><span class="status">${esc(data.commands?.length || 0)} shown</span></div>
      <div class="autonomous-history">${(data.commands || []).map(command => `<div class="warm-row"><div><b>${esc(command.intent || 'unknown')}</b><p class="meta">${esc(command.command_id || 'Not observed')} · revision ${esc(command.revision || 'Not observed')} · ${esc(fmt(command.updated_at))}</p><p class="meta">Audit ${esc(command.audit_id || 'Not observed')} · ${esc(command.reason_code || 'No reason code')}</p></div><span class="status" data-tone="${['failed','rejected','cancelled'].includes(command.status) ? 'danger' : 'ready'}">${esc(command.status || 'unknown')}</span></div>`).join('') || '<p class="empty">No commands are visible in this bounded audit.</p>'}</div>
      ${data.history_truncated ? '<p class="warning autonomous-history-note">History is bounded; older commands exist.</p>' : ''}
      <p class="meta autonomous-state-note">Current autonomous state: ${esc(autonomousStateLabel(data.effective_state))}</p>
    </article>`;
    }).join('');
    return `<section class="autonomous-grid activity-audits" aria-label="Autonomous Butler activity">${cards}</section>`;
  }

  globalThis.FleetViewModules.register({
    id: 'activity',
    owns: [
      'recent activity',
      'butler activity',
      'activity empty states'
    ],
    render(context) {
      useContext(context);
      return renderWarmActivity() + renderAutonomousActivity();
    }
  });
  loadActivityStyles();
})();
