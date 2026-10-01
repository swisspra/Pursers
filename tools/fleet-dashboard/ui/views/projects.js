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
    const ready = (board.tickets || []).filter(ticket => ticket.delivery?.state === 'integration_merged').length;
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
      ${ready ? `<p><b>${esc(ready)}</b> visible ticket${ready === 1 ? '' : 's'} ready for your team on the delivery branch.</p>` : ''}
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

  function redactedRegistryEntry(entry) {
    if (!entry) return 'Not registered';
    const visible = {};
    for (const key of Object.keys(entry).sort()) {
      const value = entry[key];
      if (key === 'work_dir') visible[key] = '[local folder configured]';
      else if (key === 'repository_url') visible[key] = '[Git source configured]';
      else if (key === 'fleet_clone_dir') visible[key] = '[Fleet clone configured]';
      else if (['board_id', 'integration_ref', 'status'].includes(key) && ['string', 'number', 'boolean'].includes(typeof value)) visible[key] = value;
      else visible[key] = '[configured]';
    }
    return JSON.stringify(visible);
  }

  function operationList(plan) {
    return (plan.operations || []).map(operation => {
      const registryImpact = operation.operation_id === 'registry'
        ? `<dl class="projects-operation-impact"><div><dt>Before</dt><dd><code>${esc(redactedRegistryEntry(operation.before))}</code></dd></div><div><dt>After</dt><dd><code>${esc(redactedRegistryEntry(operation.after))}</code></dd></div>${operation.changed_fields?.length ? `<div><dt>Changed fields</dt><dd>${esc(operation.changed_fields.join(', '))}</dd></div>` : ''}</dl>`
        : '';
      return `<li><b>${esc(operation.effect)}</b> ${esc(operation.target)}<span>${esc(operation.required_permission || '')}</span>${registryImpact}</li>`;
    }).join('');
  }

  function lifecyclePreview() {
    if (!lifecyclePlan) return '';
    const blockers = (lifecyclePlan.blockers || []).map(item => `<li>${esc(item)}</li>`).join('');
    const warnings = (lifecyclePlan.warnings || []).map(item => `<li>${esc(item)}</li>`).join('');
    const preserved = (lifecyclePlan.preserved || []).map(item => `<li>${esc(item)}</li>`).join('');
    return `<section class="card projects-lifecycle-preview" aria-live="polite">
      <div class="projects-lifecycle-head"><div><p class="projects-detail-label">Impact preview</p><h2>${lifecyclePlan.kind === 'project-remove' ? 'Remove from Fleet' : lifecyclePlan.kind === 'project-delivery' || lifecyclePlan.kind === 'project-delivery-policy' ? 'Configure delivery' : 'Add project'} · ${esc(lifecyclePlan.project)}</h2></div><span class="status ${lifecyclePlan.blocked ? 'danger' : 'ready'}">${lifecyclePlan.blocked ? 'Blocked' : 'Ready to confirm'}</span></div>
      <p class="meta">Plan expires ${esc(lifecyclePlan.expires_at)} · ${esc(lifecyclePlan.central)}</p>
      ${lifecyclePlan.delivery_workflow ? `<p class="delivery-route">Read-only base <b>${esc(lifecyclePlan.delivery_workflow.base_branch)}</b> → ticket branches → <b>${esc(lifecyclePlan.delivery_workflow.integration_branch)}</b> → your team handles the final merge</p><p>Automatic integration: <b>${lifecyclePlan.delivery_workflow.auto_integrate ? 'enabled, subject to validation' : 'disabled'}</b>. Collection: <b>${lifecyclePlan.delivery_workflow.collection_paused ? 'paused' : 'open'}</b>.</p>` : ''}
      ${lifecyclePlan.delivery_policy ? `<p class="delivery-route">Effective policy: <b>${esc(lifecyclePlan.delivery_policy.mode)}</b> · mapped base <b>${esc(lifecyclePlan.delivery_policy.mapped_base)}</b> · release <b>${esc(lifecyclePlan.delivery_policy.release_trigger?.kind)}</b> · final merge <b>manual</b></p>` : ''}
      ${lifecyclePlan.affected_projects?.length ? `<p>Affected repositories: ${esc(lifecyclePlan.affected_projects.map(item => item.project).join(', '))}</p>` : ''}
      ${lifecyclePlan.use_as_default ? '<p><b>This also sets the workflow default for newly onboarded repositories.</b></p>' : ''}
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

  const deliveryRows = new Map();
  const deliveryDrafts = new Map();
  const deliverySelections = new Map();
  let deliveryCentral = '';
  let deliveryProject = '';
  let deliveryLoadError = '';
  const deliveryLoading = new Set();

  const deliveryEditorKey = (central, project) => JSON.stringify([central, project]);
  const deliveryDraftKey = (central, project, scope, group = '') => JSON.stringify([central, project, scope, scope === 'group' ? group : '']);

  function deliveryLayer(selected, scope, group) {
    const layers = selected?.delivery_policy_layers || {};
    if (scope === 'global') return layers.global || {};
    if (scope === 'group') return layers.groups?.[group] || {};
    return layers.repository || selected?.delivery_policy_overrides || {};
  }

  function clearDeliveryDrafts(central, project = null) {
    for (const key of deliveryDrafts.keys()) {
      const [draftCentral, draftProject] = JSON.parse(key);
      if (draftCentral === central && (project === null || draftProject === project)) deliveryDrafts.delete(key);
    }
    for (const key of deliverySelections.keys()) {
      const [draftCentral, draftProject] = JSON.parse(key);
      if (draftCentral === central && (project === null || draftProject === project)) deliverySelections.delete(key);
    }
  }

  function deliveryEditor() {
    const central = deliveryCentral || centralLabels[0];
    const rows = deliveryRows.get(central) || [];
    const selected = rows.find(row => row.name === deliveryProject) || rows[0];
    const editorKey = deliveryEditorKey(central, selected?.name);
    const selection = deliverySelections.get(editorKey) || {};
    const scope = selection.scope || 'repository';
    const group = selection.group ?? selected?.delivery_policy_group ?? '';
    const draft = deliveryDrafts.get(deliveryDraftKey(central, selected?.name, scope, group));
    const policy = draft || deliveryLayer(selected, scope, group);
    const effective = selected?.delivery_policy || {};
    const integration = policy.integration_branch ?? '';
    const base = policy.mapped_base ?? '';
    const trigger = policy.release_trigger || {};
    const validation = policy.validation || {};
    const runtime = selected?.delivery_runtime || {ready: true, blockers: []};
    const provenance = selected?.delivery_policy_provenance || {};
    const preset = selection.preset || 'custom';
    const targetMode = !Object.hasOwn(policy, 'final_pr_target') ? 'inherit' : (policy.final_pr_target === null ? 'none' : 'branch');
    const commandsMode = Object.hasOwn(validation, 'test_commands') ? 'override' : 'inherit';
    const booleanValue = name => Object.hasOwn(policy, name) ? String(policy[name]) : '';
    return `<section class="card projects-delivery" id="project-delivery" aria-labelledby="delivery-title">
      <div class="projects-lifecycle-head"><div><h2 id="delivery-title">Delivery policy</h2><p>Choose how reviewed work is handed off. Your team owns the final merge. Unsupported runtime choices remain drafts and never fall back silently.</p></div><span class="status ${runtime.ready ? 'ready' : 'warning'}">${runtime.ready ? 'Runtime ready' : 'Draft only'}</span></div>
      ${deliveryLoadError ? `<p role="alert" class="error">${esc(deliveryLoadError)}</p>` : ''}
      <form id="project-delivery-form" class="projects-lifecycle-form">
        <label>Coordinator<select name="central">${centralLabels.map(label => `<option value="${esc(label)}" ${label === central ? 'selected' : ''}>${esc(label)}</option>`).join('')}</select></label>
        <label>Repository project<select name="name" required>${rows.map(row => `<option value="${esc(row.name)}" ${row === selected ? 'selected' : ''}>${esc(row.name)}</option>`).join('') || '<option value="">Load registered repositories</option>'}</select></label>
        <label>Control level<select name="scope"><option value="repository" ${scope === 'repository' ? 'selected' : ''}>Repository override</option><option value="group" ${scope === 'group' ? 'selected' : ''}>Named group default</option><option value="global" ${scope === 'global' ? 'selected' : ''}>Global default</option></select></label>
        <label>Preset<select name="preset"><option value="custom" ${preset === 'custom' ? 'selected' : ''}>Custom</option><option value="review-each-ticket" ${preset === 'review-each-ticket' ? 'selected' : ''}>Review each ticket</option><option value="receive-batches" ${preset === 'receive-batches' ? 'selected' : ''}>Receive batches</option><option value="branch-only" ${preset === 'branch-only' ? 'selected' : ''}>Branch only</option></select></label>
        <label>Named group<input name="delivery_policy_group" value="${esc(group)}" pattern="[A-Za-z0-9][A-Za-z0-9._-]{0,79}" placeholder="backend-services"><small>Explicit reference only; repository names are never inferred as groups.</small></label>
        <label>Mode<select name="mode"><option value="" ${!policy.mode ? 'selected' : ''}>Inherit</option><option value="per_ticket_pr" ${policy.mode === 'per_ticket_pr' ? 'selected' : ''}>Per-ticket PR</option><option value="batch_pr" ${policy.mode === 'batch_pr' ? 'selected' : ''}>Batched delivery PR</option><option value="branch_only" ${policy.mode === 'branch_only' ? 'selected' : ''}>Branch only</option></select></label>
        <div class="projects-wide delivery-route" role="status" aria-live="polite">Effective route: mapped base <b>${esc(effective.mapped_base || selected?.integration_ref || '')}</b> → snapshot prefix <b>${esc(effective.snapshot_branch_prefix || 'codex')}</b> → <b>${esc(effective.integration_branch || 'pursers-integration')}</b> → final target <b>${esc(effective.final_pr_target || 'none')}</b></div>
        <label>Delivery branch<input name="integration_branch" value="${esc(integration)}" maxlength="200" placeholder="Inherit"><small>Pursers collects reviewed work here.</small></label>
        <label>Mapped base branch<input name="base_branch" value="${esc(base)}" maxlength="200" placeholder="Inherit"><small>Read only: Pursers never merges back into this branch.</small></label>
        <label>Snapshot branch prefix<input name="snapshot_branch_prefix" value="${esc(policy.snapshot_branch_prefix || '')}" maxlength="200" placeholder="Inherit"></label>
        <label>Final PR target behavior<select name="final_pr_target_mode"><option value="inherit" ${targetMode === 'inherit' ? 'selected' : ''}>Inherit</option><option value="none" ${targetMode === 'none' ? 'selected' : ''}>No final PR target</option><option value="branch" ${targetMode === 'branch' ? 'selected' : ''}>Use branch below</option></select></label>
        <label>Final PR target<input name="final_pr_target" value="${esc(policy.final_pr_target || '')}" maxlength="200" placeholder="Inherit"><small>Choose “No final PR target” for branch-only delivery.</small></label>
        <label>Release trigger<select name="release_trigger"><option value="" ${!trigger.kind ? 'selected' : ''}>Inherit</option><option value="ready" ${trigger.kind === 'ready' ? 'selected' : ''}>Ready</option><option value="manual" ${trigger.kind === 'manual' ? 'selected' : ''}>Manual</option><option value="scheduled" ${trigger.kind === 'scheduled' ? 'selected' : ''}>Scheduled</option></select></label>
        <label>PR updates<select name="pr_update"><option value="" ${!policy.pr_update ? 'selected' : ''}>Inherit</option><option value="rolling" ${policy.pr_update === 'rolling' ? 'selected' : ''}>Rolling</option><option value="freeze_on_ready" ${policy.pr_update === 'freeze_on_ready' ? 'selected' : ''}>Freeze when ready</option></select></label>
        <label>Timezone<input name="timezone" value="${esc(trigger.timezone || '')}" placeholder="Asia/Bangkok"></label>
        <label>Schedule<input name="schedule" value="${esc(trigger.schedule || '')}" placeholder="0 9 * * 1-5"></label>
        <label>Automatic integration<select name="auto_integrate"><option value="" ${booleanValue('auto_integrate') === '' ? 'selected' : ''}>Inherit</option><option value="true" ${booleanValue('auto_integrate') === 'true' ? 'selected' : ''}>Enabled</option><option value="false" ${booleanValue('auto_integrate') === 'false' ? 'selected' : ''}>Disabled</option></select></label>
        <label>Collection state<select name="collection_paused"><option value="" ${booleanValue('collection_paused') === '' ? 'selected' : ''}>Inherit</option><option value="false" ${booleanValue('collection_paused') === 'false' ? 'selected' : ''}>Collecting</option><option value="true" ${booleanValue('collection_paused') === 'true' ? 'selected' : ''}>Paused</option></select></label>
        <label class="projects-check"><input name="activate" type="checkbox" ${selection.activate && scope === 'repository' ? 'checked' : ''} ${scope === 'repository' ? '' : 'disabled'}> Activate only if every selected capability is installed</label>
        <details class="projects-wide"><summary>Advanced settings</summary>
          <label>Validation commands behavior<select name="test_commands_mode"><option value="inherit" ${commandsMode === 'inherit' ? 'selected' : ''}>Inherit</option><option value="override" ${commandsMode === 'override' ? 'selected' : ''}>Override (empty is deliberate)</option></select></label>
          <label>Validation commands<textarea name="test_commands" rows="3" placeholder="pytest -q tests/unit">${esc((validation.test_commands || []).join('\n'))}</textarea></label>
          <label>Required reviewers<input name="required_reviewers" type="number" min="1" max="10" value="${esc(validation.required_reviewers || '')}" placeholder="Inherit"></label>
          <label>Conflict policy<select name="conflict_policy"><option value="" ${!policy.conflict_policy ? 'selected' : ''}>Inherit</option><option value="pause" ${policy.conflict_policy === 'pause' ? 'selected' : ''}>Pause</option><option value="repair_then_review" ${policy.conflict_policy === 'repair_then_review' ? 'selected' : ''}>Repair, then review again</option></select></label>
          <p>Final merge: <b>manual</b>. Independent review and required upstream policies cannot be disabled.</p>
        </details>
        <div class="projects-wide"><p><b>Editing exact ${esc(scope)} overrides.</b> Inheritance: global → ${esc(group || 'no group')} → repository. <code>${esc(JSON.stringify(provenance))}</code></p>${runtime.blockers?.length ? `<p class="warning">Not ready: ${esc(runtime.blockers.join('; '))}</p>` : ''}</div>
        <p class="projects-wide muted">Shared default edits preview every affected repository but stay configuration-only. Existing PRs and in-flight batches keep their targets.</p>
        <div class="projects-wide card-actions"><button class="primary-action" type="submit" ${!selected?.repository_configured ? 'disabled' : ''}>Preview delivery changes</button><button class="button" type="button" data-delivery-reset>Reset repository overrides to inherit</button><button class="button" type="button" data-delivery-refresh>Reload repositories</button></div>
      </form>
    </section>`;
  }

  async function loadDelivery(central, force = false) {
    if (deliveryLoading.has(central) || (!force && deliveryRows.has(central))) return;
    deliveryLoading.add(central);
    try {
      const response = await fetch(`/api/projects/delivery?central=${encodeURIComponent(central)}`);
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      deliveryRows.set(central, payload.projects || []);
      deliveryLoadError = '';
    } catch (error) {
      deliveryRows.set(central, []);
      deliveryLoadError = `Could not load delivery settings: ${error.message}. Use Reload repositories to retry.`;
    } finally { deliveryLoading.delete(central); }
    rerender();
  }

  function bindDelivery(root) {
    const form = root.querySelector('#project-delivery-form');
    if (!form) return;
    const central = form.elements.central.value;
    const project = form.elements.name.value || undefined;
    const editorKey = deliveryEditorKey(central, project);
    const initialSelection = deliverySelections.get(editorKey) || {};
    const renderedScope = form.elements.scope.value;
    const renderedGroup = initialSelection.group ?? form.elements.delivery_policy_group.value;
    const rows = deliveryRows.get(central) || [];
    const selected = rows.find(row => row.name === project);
    const sourcePolicy = deliveryDrafts.get(deliveryDraftKey(central, project, renderedScope, renderedGroup))
      || deliveryLayer(selected, renderedScope, renderedGroup);

    const policyFromFields = (fields, trim = false) => {
      const policy = {};
      const text = name => trim ? fields[name].value.trim() : fields[name].value;
      if (fields.mode.value) policy.mode = fields.mode.value;
      for (const [field, key] of [['integration_branch', 'integration_branch'], ['base_branch', 'mapped_base'], ['snapshot_branch_prefix', 'snapshot_branch_prefix']]) {
        if (text(field)) policy[key] = text(field);
      }
      if (fields.final_pr_target_mode.value === 'none') policy.final_pr_target = null;
      if (fields.final_pr_target_mode.value === 'branch' && text('final_pr_target')) policy.final_pr_target = text('final_pr_target');
      if (fields.release_trigger.value) {
        policy.release_trigger = {kind: fields.release_trigger.value};
        if (fields.release_trigger.value === 'scheduled') {
          policy.release_trigger.timezone = text('timezone');
          policy.release_trigger.schedule = text('schedule');
        }
      }
      if (fields.pr_update.value) policy.pr_update = fields.pr_update.value;
      for (const name of ['auto_integrate', 'collection_paused']) {
        if (fields[name].value) policy[name] = fields[name].value === 'true';
      }
      const validation = {};
      if (fields.test_commands_mode.value === 'override') {
        validation.test_commands = fields.test_commands.value.split('\n').map(value => trim ? value.trim() : value).filter(Boolean);
      }
      if (fields.required_reviewers.value) validation.required_reviewers = Number(fields.required_reviewers.value);
      for (const required of ['independent_review', 'require_upstream_policies']) {
        if (sourcePolicy.validation?.[required] === true) validation[required] = true;
      }
      if (Object.keys(validation).length) policy.validation = validation;
      if (fields.conflict_policy.value) policy.conflict_policy = fields.conflict_policy.value;
      if (sourcePolicy.final_merge === 'manual') policy.final_merge = 'manual';
      return policy;
    };

    const saveDraft = (scope = renderedScope, group = renderedGroup) => {
      const fields = form.elements;
      deliveryDrafts.set(deliveryDraftKey(central, project, scope, group), policyFromFields(fields));
      deliverySelections.set(editorKey, {
        scope,
        group: fields.delivery_policy_group.value,
        preset: fields.preset.value,
        activate: scope === 'repository' && fields.activate.checked,
      });
    };
    for (const name of ['mode','integration_branch','base_branch','snapshot_branch_prefix','final_pr_target_mode','final_pr_target','release_trigger','pr_update','timezone','schedule','auto_integrate','collection_paused','test_commands_mode','test_commands','required_reviewers','conflict_policy']) {
      form.elements[name].addEventListener('input', () => saveDraft());
      form.elements[name].addEventListener('change', () => saveDraft());
    }
    loadDelivery(central);
    const rerenderForLayerChange = () => {
      delete form.dataset.dirty;
      form.querySelector(':focus')?.blur();
      rerender();
    };
    form.elements.central.addEventListener('change', () => {
      deliveryCentral = form.elements.central.value; deliveryProject = ''; rerenderForLayerChange();
    });
    form.elements.name.addEventListener('change', () => { deliveryProject = form.elements.name.value; rerenderForLayerChange(); });
    form.elements.scope.addEventListener('change', () => {
      saveDraft(renderedScope, renderedGroup);
      deliverySelections.set(editorKey, {
        ...deliverySelections.get(editorKey), scope: form.elements.scope.value,
        preset: 'custom', activate: false,
      });
      rerenderForLayerChange();
    });
    form.elements.delivery_policy_group.addEventListener('change', () => {
      saveDraft(renderedScope, renderedGroup);
      deliverySelections.set(editorKey, {
        ...deliverySelections.get(editorKey), group: form.elements.delivery_policy_group.value,
      });
      if (renderedScope === 'group') rerenderForLayerChange();
    });
    form.elements.activate.addEventListener('change', () => saveDraft());
    form.elements.preset.addEventListener('change', () => {
      const preset = form.elements.preset.value;
      if (preset === 'review-each-ticket') { form.elements.mode.value = 'per_ticket_pr'; form.elements.release_trigger.value = 'ready'; form.elements.final_pr_target_mode.value = 'branch'; form.elements.final_pr_target.value = form.elements.base_branch.value; }
      if (preset === 'receive-batches') { form.elements.mode.value = 'batch_pr'; form.elements.release_trigger.value = 'manual'; form.elements.final_pr_target_mode.value = 'branch'; form.elements.final_pr_target.value = form.elements.base_branch.value; }
      if (preset === 'branch-only') { form.elements.mode.value = 'branch_only'; form.elements.release_trigger.value = 'ready'; form.elements.final_pr_target_mode.value = 'none'; form.elements.final_pr_target.value = ''; }
      saveDraft();
    });
    form.querySelector('[data-delivery-refresh]').addEventListener('click', () => loadDelivery(central, true));
    form.querySelector('[data-delivery-reset]').addEventListener('click', () => requestPlan({
      action: 'delivery', scope: form.elements.scope.value, name: form.elements.name.value,
      delivery_policy_group: form.elements.delivery_policy_group.value || null,
      reset_to_inherit: true,
    }, central));
    form.addEventListener('submit', event => {
      event.preventDefault();
      const fields = form.elements;
      const branches = ['integration_branch','base_branch'].map(key => fields[key].value.trim()).filter(Boolean);
      if (branches.length === 2 && new Set(branches.map(branch => branch.toLowerCase())).size !== 2) {
        fields.integration_branch.setCustomValidity('Delivery and mapped base must be different branches.');
        fields.integration_branch.reportValidity();
        fields.integration_branch.setCustomValidity('');
        return;
      }
      requestPlan({action: 'delivery', scope: fields.scope.value, name: fields.name.value,
        delivery_policy_group: fields.delivery_policy_group.value || null,
        activate: fields.scope.value === 'repository' && fields.activate.checked,
        delivery_policy: policyFromFields(fields, true)}, central);
    });
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

    return `${pageHead('Projects', 'Your project map', 'See which coordinator owns each board, where work is moving, and what needs attention.', action)}${warmTruthStrip()}${lifecycleOutcome()}${lifecyclePreview()}${deliveryEditor()}${addProjectForm()}${summary}<div class="projects-map">${content}</div>`;
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
    bindDelivery(root);
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
        deliveryRows.delete(plan.central);
        clearDeliveryDrafts(plan.central, plan.scope === 'repository' ? plan.project : null);
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
