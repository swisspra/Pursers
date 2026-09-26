/* Fleet route module: projects. */
(function registerProjectsView() {
  'use strict';

  let esc, pageHead, warmTruthStrip, warmBoards, numberCount, boardHref;

  const STYLE_URL = '/ui/views/projects.css';
  const ACTIVE_STATES = new Set(['claimed', 'in_progress', 'creating_report']);
  const REVIEW_STATES = new Set(['submitted', 'reviewing', 'in_review']);

  function loadStyles() {
    if (typeof document === 'undefined') return;
    if (document.querySelector('link[data-fleet-view-style="projects"]')) return;
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = STYLE_URL;
    link.dataset.fleetViewStyle = 'projects';
    document.head.append(link);
  }

  function useContext(context) {
    ({esc, pageHead, warmTruthStrip, warmBoards, numberCount, boardHref} = context);
  }

  function sumStates(counts, states) {
    return Object.entries(counts).reduce(
      (sum, [state, value]) => sum + (states.has(state) ? numberCount(value) : 0),
      0,
    );
  }

  function projectHealth(board) {
    const source = String(board.status || 'unknown');
    const unavailable = Boolean(board.error) || ['error', 'unavailable'].includes(source);
    return {
      label: unavailable ? 'needs attention' : source.replaceAll('_', ' '),
      tone: unavailable ? 'danger' : source === 'ready' ? 'ready' : 'neutral',
    };
  }

  function statePills(counts) {
    const entries = Object.entries(counts);
    if (!entries.length) return '<span class="projects-no-state">No ticket states reported</span>';
    return entries.map(([key, value]) => (
      `<span class="pill"><span>${esc(key.replaceAll('_', ' '))}</span><b>${esc(value)}</b></span>`
    )).join('');
  }

  function projectCard(central, board) {
    const counts = board.counts || {};
    const total = Object.values(counts).reduce((sum, value) => sum + numberCount(value), 0);
    const active = sumStates(counts, ACTIVE_STATES);
    const review = sumStates(counts, REVIEW_STATES);
    const open = numberCount(counts.open);
    const health = projectHealth(board);
    const truncation = board.snapshot_truncation;
    const limited = truncation && truncation.total > truncation.returned;
    const scope = limited
      ? `Showing ${esc(truncation.returned)} of ${esc(truncation.total)} tickets in this bounded snapshot.`
      : `${esc(total)} tickets visible in this bounded snapshot.`;
    const error = board.error
      ? `<p class="projects-error error">Connection detail: ${esc(board.error)}</p>`
      : '';

    return `<article class="board-card" data-board-id="${esc(board.board_id)}" data-projects-card data-central="${esc(central)}" data-pursers-board="${esc(board.board_id)}" data-pursers-status="${esc(board.status || 'unknown')}">
      <header class="projects-board-heading">
        <span class="projects-signal" data-tone="${esc(health.tone)}" aria-hidden="true"></span>
        <div>
          <h3>${esc(board.label)}</h3>
          <p class="projects-route"><span>${esc(central)}</span><span aria-hidden="true">/</span><code>${esc(board.board_id)}</code></p>
        </div>
        <span class="status projects-health" data-tone="${esc(health.tone)}">${esc(health.label)}</span>
      </header>
      <dl class="projects-work" aria-label="Work snapshot for ${esc(board.label)}">
        <div><dt>Ready to start</dt><dd>${esc(open)}</dd></div>
        <div><dt>In progress</dt><dd>${esc(active)}</dd></div>
        <div><dt>Review ready</dt><dd>${esc(review)}</dd></div>
      </dl>
      <div class="projects-board-detail">
        <div>
          <p class="projects-detail-label">All reported states</p>
          <div class="counts projects-state-list">${statePills(counts)}</div>
        </div>
        <div class="projects-board-actions">
          <a class="primary-action" href="${boardHref(central, board.board_id)}">Open project</a>
          <a class="button" href="${boardHref(central, board.board_id, 'routes')}">View routes</a>
        </div>
      </div>
      ${error}<p class="projects-scope meta">${scope}</p>
    </article>`;
  }

  function centralGroup(central, boards) {
    return `<section class="projects-central">
      <header class="projects-central-heading">
        <div>
          <p class="projects-detail-label">Coordinator</p>
          <h3>${esc(central)}</h3>
        </div>
        <span class="status">${esc(boards.length)} ${boards.length === 1 ? 'project' : 'projects'}</span>
      </header>
      <div class="boards-list">${boards.map(board => projectCard(central, board)).join('')}</div>
    </section>`;
  }

  function emptyProjects() {
    return `<section class="empty-guidance projects-empty">
      <div class="projects-empty-mark" aria-hidden="true">+</div>
      <h3>Connect your first project</h3>
      <p>Add project uses the existing guarded Connections flow. You will review the board, protected doors, policy defaults, and Fleet clone before work begins.</p>
      <div class="card-actions"><a class="primary-action" href="#/seats">Add project</a></div>
    </section>`;
  }

  function renderWarmProjects() {
    const rows = warmBoards();
    const groups = new Map();
    for (const {central, board} of rows) {
      const boards = groups.get(central) || [];
      boards.push(board);
      groups.set(central, boards);
    }
    const active = rows.reduce(
      (sum, {board}) => sum + sumStates(board.counts || {}, ACTIVE_STATES),
      0,
    );
    const review = rows.reduce(
      (sum, {board}) => sum + sumStates(board.counts || {}, REVIEW_STATES),
      0,
    );
    const action = '<a class="primary-action projects-add" href="#/seats">+ Add project</a>';
    const summary = rows.length ? `<section class="projects-summary" aria-label="Projects summary">
      <p><strong>${esc(rows.length)} ${rows.length === 1 ? 'project' : 'projects'}</strong> connected through <strong>${esc(groups.size)} ${groups.size === 1 ? 'coordinator' : 'coordinators'}</strong>.</p>
      <dl><div><dt>In progress</dt><dd>${esc(active)}</dd></div><div><dt>Review ready</dt><dd>${esc(review)}</dd></div></dl>
    </section>` : '';
    const content = rows.length
      ? [...groups.entries()].map(([central, boards]) => centralGroup(central, boards)).join('')
      : emptyProjects();

    return `${pageHead('Projects', 'Your project map', 'See which coordinator owns each board, where work is moving, and what needs attention.', action)}${warmTruthStrip()}${summary}<div class="projects-map">${content}</div>`;
  }

  loadStyles();
  globalThis.FleetViewModules.register({
    id: 'projects',
    owns: [
      'project and board cards',
      'workspace links',
      'project empty states',
    ],
    render(context) { useContext(context); return renderWarmProjects(); },
  });
})();
