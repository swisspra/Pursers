/* Fleet route module: projects. */
(function registerProjectsView() {
  'use strict';

  let esc, pageHead, warmTruthStrip, warmBoards, numberCount, boardHref, centralLabels;
  let lifecyclePlan = null;
  let lifecycleResult = null;
  let lifecycleError = '';

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
    ({esc, pageHead, warmTruthStrip, warmBoards, numberCount, boardHref, centralLabels} = context);
    centralLabels = centralLabels || [...new Set(warmBoards().map(row => row.central))];
    if (!centralLabels.length) centralLabels = ['default'];
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
          <button class="button projects-remove" type="button" data-project-remove="${esc(board.label)}" data-central="${esc(central)}">Remove from Fleet</button>
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
      <div class="card-actions"><button class="primary-action" type="button" data-project-add-open>Add project</button></div>
    </section>`;
  }

  function addProjectForm() {
    return `<section class="card projects-lifecycle" id="project-lifecycle" aria-labelledby="project-lifecycle-title">
      <div class="projects-lifecycle-head"><div><p class="projects-detail-label">Guarded lifecycle</p><h2 id="project-lifecycle-title">Add a project</h2></div><span class="status">Preview first</span></div>
      <p class="muted">Choose a local folder and optional Git source. Planning is read-only; apply runs once only after you review board, registry, clone, and permission effects.</p>
      <form id="project-lifecycle-form" class="projects-lifecycle-form">
        <label>Coordinator<select name="central">${centralLabels.map(label => `<option value="${esc(label)}">${esc(label)}</option>`).join('')}</select></label>
        <label>Project name<input name="name" required maxlength="120" placeholder="my-service"></label>
        <label>Board ID<input name="board_id" required pattern="[A-Za-z0-9._-]{1,80}" placeholder="my-service"></label>
        <label class="projects-wide">Local folder (absolute path)<input name="work_dir" required placeholder="/PATH/TO/project" autocomplete="off"></label>
        <label>Git source<select name="git_mode"><option value="existing">Existing checkout</option><option value="none">No Git</option><option value="clone">Clone from remote</option></select></label>
        <label>Integration ref<input name="integration_ref" value="main" required></label>
        <label class="projects-wide" data-project-repository>Repository URL (optional for existing checkout)<input name="repository_url" placeholder="https://example.invalid/project.git" autocomplete="off"></label>
        <label class="projects-check"><input name="prepare_fleet_clone" type="checkbox" checked> Prepare a separate Fleet clone</label>
        <div class="projects-wide"><button class="primary-action" type="submit">Preview effects</button></div>
      </form>
    </section>`;
  }

  function operationList(plan) {
    return (plan.operations || []).map(operation => `<li><b>${esc(operation.effect)}</b> ${esc(operation.target)}<span>${esc(operation.required_permission || '')}</span></li>`).join('');
  }

  function lifecyclePreview() {
    if (!lifecyclePlan) return '';
    const blockers = (lifecyclePlan.blockers || []).map(item => `<li>${esc(item)}</li>`).join('');
    const warnings = (lifecyclePlan.warnings || []).map(item => `<li>${esc(item)}</li>`).join('');
    const preserved = (lifecyclePlan.preserved || []).map(item => `<li>${esc(item)}</li>`).join('');
    return `<section class="card projects-lifecycle-preview" aria-live="polite">
      <div class="projects-lifecycle-head"><div><p class="projects-detail-label">Impact preview</p><h2>${lifecyclePlan.kind === 'project-remove' ? 'Remove from Fleet' : 'Add project'} · ${esc(lifecyclePlan.project)}</h2></div><span class="status ${lifecyclePlan.blocked ? 'danger' : 'ready'}">${lifecyclePlan.blocked ? 'Blocked' : 'Ready to confirm'}</span></div>
      <p class="meta">Plan expires ${esc(lifecyclePlan.expires_at)} · ${esc(lifecyclePlan.central)}</p>
      <h3>Effects</h3><ol class="projects-operation-list">${operationList(lifecyclePlan)}</ol>
      ${blockers ? `<div class="error"><b>Resolve before apply</b><ul>${blockers}</ul></div>` : ''}
      ${warnings ? `<div class="warning"><b>Guardrails</b><ul>${warnings}</ul></div>` : ''}
      ${preserved ? `<div class="projects-preserved"><b>Always preserved</b><ul>${preserved}</ul></div>` : ''}
      <p><b>Rollback:</b> ${esc((lifecyclePlan.rollback || []).join(' '))}</p>
      <form id="project-lifecycle-apply" class="projects-confirm">
        <label>Type <code>${esc(lifecyclePlan.confirmation)}</code> to confirm<input name="confirmation" autocomplete="off" required></label>
        <button class="primary-action" type="submit" ${lifecyclePlan.blocked ? 'disabled' : ''}>Confirm and apply once</button>
        <button class="button" type="button" data-project-plan-cancel>Cancel</button>
      </form>
    </section>`;
  }

  function lifecycleOutcome() {
    if (!lifecycleResult && !lifecycleError) return '';
    return `<section class="card projects-lifecycle-result" role="status">
      <h2>${lifecycleError ? 'Project change did not apply' : 'Project change applied'}</h2>
      <p class="${lifecycleError ? 'error' : 'status ready'}">${esc(lifecycleError || `${lifecycleResult.kind} completed for ${lifecycleResult.project}.`)}</p>
      ${lifecycleResult?.preserved ? `<p class="muted">Preserved: ${esc(lifecycleResult.preserved.join('; '))}</p>` : ''}
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
    const action = '<button class="primary-action projects-add" type="button" data-project-add-open>+ Add project</button>';
    const summary = rows.length ? `<section class="projects-summary" aria-label="Projects summary">
      <p><strong>${esc(rows.length)} ${rows.length === 1 ? 'project' : 'projects'}</strong> connected through <strong>${esc(groups.size)} ${groups.size === 1 ? 'coordinator' : 'coordinators'}</strong>.</p>
      <dl><div><dt>In progress</dt><dd>${esc(active)}</dd></div><div><dt>Review ready</dt><dd>${esc(review)}</dd></div></dl>
    </section>` : '';
    const content = rows.length
      ? [...groups.entries()].map(([central, boards]) => centralGroup(central, boards)).join('')
      : emptyProjects();

    return `${pageHead('Projects', 'Your project map', 'See which coordinator owns each board, where work is moving, and what needs attention.', action)}${warmTruthStrip()}${lifecycleOutcome()}${lifecyclePreview()}${addProjectForm()}${summary}<div class="projects-map">${content}</div>`;
  }

  async function postLifecycle(path, central, payload) {
    const response = await fetch(`${path}?central=${encodeURIComponent(central)}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    let body = {};
    try { body = await response.json(); } catch (_error) { /* bounded below */ }
    if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
    return body;
  }

  function rerender() {
    if (typeof renderHub === 'function') renderHub();
  }

  async function requestPlan(payload, central) {
    lifecycleError = '';
    lifecycleResult = null;
    try {
      lifecyclePlan = await postLifecycle('/api/lifecycle/plan', central, payload);
    } catch (error) {
      lifecyclePlan = null;
      lifecycleError = `Preview failed: ${error.message}`;
    }
    rerender();
  }

  function syncGitMode(form) {
    const mode = form.elements.git_mode.value;
    const repository = form.elements.repository_url;
    repository.required = mode === 'clone';
    const clone = form.elements.prepare_fleet_clone;
    if (mode === 'none') {
      clone.checked = false;
      clone.disabled = true;
    } else {
      clone.disabled = false;
    }
  }

  function bindProjects(_context, root) {
    const addForm = root.querySelector('#project-lifecycle-form');
    if (addForm) {
      syncGitMode(addForm);
      addForm.elements.git_mode.addEventListener('change', () => syncGitMode(addForm));
      addForm.addEventListener('submit', event => {
        event.preventDefault();
        const form = event.currentTarget;
        const payload = {
          action: 'add',
          name: form.elements.name.value.trim(),
          board_id: form.elements.board_id.value.trim(),
          work_dir: form.elements.work_dir.value.trim(),
          git_mode: form.elements.git_mode.value,
          repository_url: form.elements.repository_url.value.trim() || null,
          integration_ref: form.elements.integration_ref.value.trim() || 'main',
          prepare_fleet_clone: form.elements.prepare_fleet_clone.checked,
        };
        requestPlan(payload, form.elements.central.value);
      });
    }
    root.querySelectorAll('[data-project-add-open]').forEach(button => button.addEventListener('click', () => root.querySelector('#project-lifecycle')?.scrollIntoView({behavior: 'smooth'})));
    root.querySelectorAll('[data-project-remove]').forEach(button => button.addEventListener('click', () => requestPlan({action: 'remove', name: button.dataset.projectRemove}, button.dataset.central)));
    root.querySelector('[data-project-plan-cancel]')?.addEventListener('click', () => { lifecyclePlan = null; lifecycleError = ''; rerender(); });
    root.querySelector('#project-lifecycle-apply')?.addEventListener('submit', async event => {
      event.preventDefault();
      const plan = lifecyclePlan;
      if (!plan || plan.blocked) return;
      const button = event.currentTarget.querySelector('[type="submit"]');
      button.disabled = true;
      try {
        lifecycleResult = await postLifecycle('/api/lifecycle/apply', plan.central, {
          plan_id: plan.plan_id,
          plan_digest: plan.plan_digest,
          confirmation: event.currentTarget.elements.confirmation.value,
        });
        lifecyclePlan = null;
        lifecycleError = '';
        if (typeof refreshCentral === 'function') await refreshCentral(plan.central);
      } catch (error) {
        lifecycleError = `Apply failed: ${error.message}`;
      }
      rerender();
    });
  }

  loadStyles();
  globalThis.FleetViewModules.register({
    id: 'projects',
    owns: [
      'project and board cards',
      'workspace links',
      'project empty states',
      'guarded project add and removal lifecycle',
    ],
    render(context) { useContext(context); return renderWarmProjects(); },
    bind(context, root) { useContext(context); bindProjects(context, root); },
  });
})();
