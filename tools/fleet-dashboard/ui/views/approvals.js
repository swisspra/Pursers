/* Fleet route module: approvals. */
(function registerApprovalsView() {
  'use strict';

  let esc, fmt, pageHead, warmTruthStrip, warmTickets, ticketHref, warmBoards,
    boardHref, renderWaitingForYou;

  const STYLE_URL = '/ui/views/approvals.css';
  const REVIEW_STATUSES = new Set(['submitted', 'reviewing', 'in_review']);

  function loadStyles() {
    if (typeof document === 'undefined') return;
    if (document.querySelector('link[data-fleet-view-style="approvals"]')) return;
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = STYLE_URL;
    link.dataset.fleetViewStyle = 'approvals';
    document.head.append(link);
  }

  function useContext(context) {
    ({esc, fmt, pageHead, warmTruthStrip, warmTickets, ticketHref, warmBoards,
      boardHref, renderWaitingForYou} = context);
  }

  function reviewRow({central, board, ticket}) {
    const updated = ticket.updated_at
      ? fmt(ticket.updated_at)
      : 'Not supplied in the bounded list';
    const rejections = Number(ticket.rejection_count || 0);
    const conflict = rejections > 0
      ? `<p class="approvals-conflict warning">${esc(rejections)} prior ${rejections === 1 ? 'rejection' : 'rejections'} recorded. Re-check the new evidence before deciding.</p>`
      : '';
    const policy = ticket.review_label
      ? `<span>Review policy · ${esc(ticket.review_label)}</span>`
      : '<span>Review policy · independent review required</span>';

    return `<article class="approvals-review-row" data-approval-review data-ticket-status="${esc(ticket.status)}">
      <header class="approvals-review-head">
        <div class="approvals-route">
          <a class="id" href="${ticketHref(central, board.board_id, ticket.id)}">${esc(ticket.id)}</a>
          <span>${esc(central)}</span><span aria-hidden="true">/</span><span>${esc(board.label)}</span>
        </div>
        <span class="status">${esc(ticket.status_label || ticket.status)}</span>
      </header>
      <div class="approvals-review-body">
        <div>
          <h4><a href="${ticketHref(central, board.board_id, ticket.id)}">${esc(ticket.title || '(untitled)')}</a></h4>
          <p>Evidence is submitted. A reviewer—not this dashboard—records approval or requests changes.</p>
        </div>
        <dl class="approvals-facts">
          <div><dt>Waiting party</dt><dd>Independent reviewer</dd></div>
          <div><dt>Evidence updated</dt><dd>${esc(updated)}</dd></div>
          <div><dt>Scope</dt><dd>${esc(board.label)} · bounded ticket view</dd></div>
        </dl>
        ${conflict}
        <div class="approvals-next">
          <div><span>Next safe step</span><p>Inspect the exact submission, tests, and limitations before the reviewer records a verdict.</p></div>
          <div><span>Consequence</span><p>Approval closes the recorded result; rejection returns the ticket for rework.</p></div>
        </div>
      </div>
      <footer class="approvals-review-actions">
        <div class="approvals-policy">${policy}</div>
        <a class="primary-action" href="${ticketHref(central, board.board_id, ticket.id)}">Inspect evidence</a>
      </footer>
    </article>`;
  }

  function reviewQueue() {
    const submitted = warmTickets().filter(({ticket}) => REVIEW_STATUSES.has(ticket.status));
    const rows = submitted.map(reviewRow).join('');
    return `<section class="approvals-review" aria-labelledby="approvals-review-title">
      <header class="approvals-section-head">
        <div><h3 id="approvals-review-title">Submitted for independent review</h3><p>Review status is separate from a human request or intake approval.</p></div>
        <span class="status">${esc(submitted.length)} ready</span>
      </header>
      <div class="approvals-review-list">${rows || '<p class="empty approvals-empty">No submitted work is waiting for review.</p>'}</div>
    </section>`;
  }

  function intakeRow({central, board}) {
    const truncation = board.snapshot_truncation;
    const bounded = truncation && truncation.total > truncation.returned
      ? `Snapshot shows ${esc(truncation.returned)} of ${esc(truncation.total)} tickets.`
      : 'Board intake and its current decisions open in the bounded workspace.';
    return `<article class="approvals-intake-row" data-approval-intake>
      <div>
        <div class="approvals-route"><span>${esc(central)}</span><span aria-hidden="true">/</span><strong>${esc(board.label)}</strong></div>
        <p>Approving a draft authorizes ticket creation; declining records the intake decision. Neither action happens on this summary page.</p>
        <span class="meta">${bounded}</span>
      </div>
      <a class="button" href="${boardHref(central, board.board_id)}">Open guarded intake</a>
    </article>`;
  }

  function intakeQueue() {
    const boards = warmBoards();
    return `<section class="approvals-intake" aria-labelledby="approvals-intake-title">
      <header class="approvals-section-head">
        <div><h3 id="approvals-intake-title">Intake decisions</h3><p>Draft acceptance and decline stay inside each project's guarded workflow.</p></div>
        <span class="status">${esc(boards.length)} ${boards.length === 1 ? 'queue' : 'queues'}</span>
      </header>
      <div class="approvals-intake-list">${boards.map(intakeRow).join('') || '<p class="empty approvals-empty">No project intake queues are connected.</p>'}</div>
    </section>`;
  }

  function renderWarmApprovals() {
    const boundary = `<aside class="approvals-boundary" aria-label="Approval boundaries">
      <strong>Decisions stay in their source workflow.</strong>
      <span>This page brings the waiting party, scope, evidence, and consequence together without adding approval or review authority.</span>
    </aside>`;
    const evidenceIntro = `<div class="approvals-evidence-intro">
      <h3>Recorded agreement signals</h3>
      <p>Only source-backed Butler marks and repeated-question history appear below. Empty evidence groups are collapsed.</p>
    </div>`;
    return `${pageHead('Approvals', 'Decisions, in safe order', 'Resolve human requests first, inspect independent review handoffs, then open guarded intake at its source.')}${warmTruthStrip()}${boundary}<div class="approvals-flow">${renderWaitingForYou()}${reviewQueue()}${intakeQueue()}${evidenceIntro}</div>`;
  }

  loadStyles();
  globalThis.FleetViewModules.register({
    id: 'approvals',
    owns: [
      'approval queue',
      'human requests',
      'approval empty states',
    ],
    render(context) { useContext(context); return renderWarmApprovals(); },
  });
})();
