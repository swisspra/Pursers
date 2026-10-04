/* Fleet route module: settings. */
(function registerSettingsView() {
  'use strict';

  let esc, relativeAge, centralLabels, centralHref, pageHead, warmTruthStrip,
    autonomousRows, autonomousStateLabel, autonomousObservationLabel,
    butlerError, butlerData, defaultCentral, warmBoards, renderHub;

  const STYLE_URL = '/ui/views/settings.css';
  const managed = new Map();
  const dispatch = new Map();
  const deliveries = new Map();
  const settledSettings = new Set();
  let seatInventory = null;
  let seatBridge = null;
  let selectedCentral = '';
  let selectedBoard = '';
  let selectedProject = '';
  let settingsMode = 'simple';
  let settingsSearch = '';
  let pendingPlan = null;
  let settingsNotice = '';
  let settingsError = '';
  let loadingKey = '';

  function loadStyles() {
    if (typeof document === 'undefined') return;
    if (document.querySelector('link[data-fleet-view-style="settings"]')) return;
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = STYLE_URL;
    link.dataset.fleetViewStyle = 'settings';
    document.head.append(link);
  }

  function useContext(context) {
    ({esc, relativeAge, centralLabels, centralHref, pageHead, warmTruthStrip,
      autonomousRows, autonomousStateLabel, autonomousObservationLabel,
      butlerError, butlerData, defaultCentral, warmBoards, renderHub} = context);
  }

  const keyFor = (central, board) => `${central}\u0000${board}`;

  async function api(path, options) {
    const response = await fetch(path, options);
    let body = {};
    try { body = await response.json(); } catch (_error) {}
    if (!response.ok) {
      const error = new Error(body.error || `HTTP ${response.status}`);
      error.status = response.status;
      throw error;
    }
    return body;
  }

  function boardScopes() {
    const rows = typeof warmBoards === 'function' ? warmBoards() : [];
    const seen = new Set();
    return rows.filter(({central, board}) => {
      const key = keyFor(central, board.board_id);
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }

  function ensureScope() {
    const scopes = boardScopes();
    if (!scopes.length) return null;
    let selected = scopes.find(row => row.central === selectedCentral && row.board.board_id === selectedBoard);
    if (!selected) selected = scopes.find(row => row.central === defaultCentral) || scopes[0];
    selectedCentral = selected.central;
    selectedBoard = selected.board.board_id;
    return selected;
  }

  function familyStatus(family) {
    const status = family?.status || 'unavailable';
    const tone = status === 'configurable' ? 'ready' : status === 'error' ? 'danger' : 'warning';
    return `<span class="status" data-tone="${tone}">${esc(status.replaceAll('_', ' '))}</span>`;
  }

  function settingField(label, name, value, attrs = '', helper = '') {
    return `<label>${esc(label)}<input name="${esc(name)}" value="${esc(value ?? '')}" ${attrs}>${helper ? `<small>${esc(helper)}</small>` : ''}</label>`;
  }

  function planPanel() {
    if (!pendingPlan) return '';
    const preview = pendingPlan.preview || {};
    const changes = preview.comparison?.changes || [];
    const summary = pendingPlan.after || preview.candidate || preview.effective || {};
    return `<aside class="settings-plan" role="region" aria-labelledby="settings-plan-title"><div><p class="settings-section-kicker">Review before apply</p><h3 id="settings-plan-title">${esc(pendingPlan.family.replaceAll('_', ' '))}</h3><p>The plan expires shortly and applies only if the source revision still matches.</p></div><pre>${esc(changes.length ? changes.map(row => `${row.change}: ${row.path}`).join('\n') : JSON.stringify(summary, null, 2))}</pre><div class="card-actions"><button class="primary-action" type="button" data-settings-apply>Apply exact plan</button><button class="button" type="button" data-settings-cancel>Cancel</button></div></aside>`;
  }

  function renderBoardPolicy(data) {
    const family = data?.families?.board_policy;
    if (!family) return '<p class="empty">Board policy is loading.</p>';
    const values = family.values || {};
    return `<form class="settings-editor" data-settings-family="board_policy" data-expected="${esc(family.expected_sha256)}"><div class="settings-editor-head"><div><h3>Review and staleness</h3><p>Effective values from Central. Saving is hot-applied after an immutable preview.</p></div>${familyStatus(family)}</div><div class="settings-fields"><label>Review policy<select name="review_policy"><option value="strict" ${values.review_policy === 'strict' ? 'selected' : ''}>Strict independent review</option><option value="workflow" ${values.review_policy === 'workflow' ? 'selected' : ''}>Workflow review</option></select><small>Source: board policy</small></label>${settingField('Stale after days', 'stale_after_days', values.stale_after_days, 'type="number" min="1" max="3650" required', 'Source: board policy · hot apply')}</div><div class="card-actions"><button class="primary-action" type="submit">Preview policy changes</button><button class="button" type="reset">Reset to effective</button></div></form>`;
  }

  function renderRetention(data) {
    const family = data?.families?.central_retention;
    if (!family || family.status !== 'configurable') return `<article class="settings-unavailable">${familyStatus(family)}<p>${esc(family?.reason || 'Retention settings are loading.')}</p></article>`;
    const labels = {
      archive_after_days: 'Archive after days', inline_history_limit: 'Inline history limit',
      invite_prune_after_days: 'Invite prune after days', journal_retention_days: 'Journal retention days',
      journal_row_cap: 'Journal row cap'
    };
    return `<form class="settings-editor" data-settings-family="central_retention" data-expected="${esc(family.expected_sha256)}"><div class="settings-editor-head"><div><h3>Retention and bounded history</h3><p>Saving values never runs archive, prune, or compaction maintenance.</p></div>${familyStatus(family)}</div><div class="settings-fields">${Object.entries(labels).map(([name, label]) => { const range = family.ranges[name]; return settingField(label, name, family.values[name], `type="number" min="${esc(range.minimum)}" max="${esc(range.maximum)}" required`, `Default ${family.defaults[name] ?? '—'} · hot apply`); }).join('')}</div><div class="settings-safety-note"><b>Separate maintenance confirmation required</b><p>These settings change future bounds only. Destructive maintenance remains a different guarded operation.</p></div><div class="card-actions"><button class="primary-action" type="submit">Preview retention changes</button><button class="button" type="reset">Reset to effective</button></div></form>`;
  }

  function renderMemberships(data) {
    const family = data?.families?.memberships;
    if (!family) return '<p class="empty">Memberships are loading.</p>';
    const rows = (family.members || []).map(row => `<tr><td><code>${esc(row.principal_id)}</code></td><td>${esc(row.role)}</td><td>${esc((row.agent_names || []).join(', ') || 'No active agent name')}</td></tr>`).join('');
    return `<div class="settings-editor"><div class="settings-editor-head"><div><h3>Memberships and roles</h3><p>Use the full principal ID. Existing agent names are context only.</p></div>${familyStatus(family)}</div><div class="table-scroll"><table><thead><tr><th>Principal</th><th>Role</th><th>Agents</th></tr></thead><tbody>${rows || '<tr><td colspan="3" class="empty">No memberships returned.</td></tr>'}</tbody></table></div><form class="settings-inline-form" data-settings-family="membership" data-expected="${esc(family.expected_sha256)}"><label>Operation<select name="operation"><option value="add">Add</option><option value="set_role">Change role</option><option value="remove">Remove</option></select></label>${settingField('Full principal ID', 'principal_id', '', 'pattern="PR-[a-f0-9]{64}" required')}<label>Role<select name="role"><option>member</option><option>reviewer</option><option>admin</option></select></label><button class="primary-action" type="submit">Preview membership change</button></form></div>`;
  }

  function sourceConnectorEditor(family) {
    if (!family || family.status !== 'configurable') return `<article class="settings-unavailable">${familyStatus(family)}<p>${esc(family?.reason || 'Connector source configuration is loading.')}</p></article>`;
    const stored = family.desired || family.effective || {};
    const doc = family.effective || stored;
    const connectors = doc.connectors || [];
    const sources = doc.sources || [];
    return `<form class="settings-editor" data-settings-family="source_connectors" data-expected="${esc(family.expected_sha256)}"><input type="hidden" name="source_document" value="${esc(JSON.stringify(stored))}"><div class="settings-editor-head"><div><h3>Connectors, sources and writeback</h3><p>Fields are validated by the resident parser. Stored secrets and private paths stay redacted and preserved.</p></div>${familyStatus(family)}</div><div class="settings-source-grid">${connectors.map((connector, index) => `<fieldset data-connector-index="${index}"><legend>${esc(connector.connector_id)}</legend><label><input type="checkbox" name="connector_${index}_enabled" ${connector.enabled ? 'checked' : ''}> Enabled</label><label>Transport<select name="connector_${index}_transport"><option value="stdio" ${connector.transport === 'stdio' ? 'selected' : ''}>stdio</option><option value="streamable_http" ${connector.transport === 'streamable_http' ? 'selected' : ''}>streamable HTTP</option></select></label>${settingField('Protocol revision', `connector_${index}_protocol_revision`, connector.protocol_revision, 'required')}${settingField('Endpoint reference', `connector_${index}_endpoint_ref`, connector.endpoint_ref, 'required')}${settingField('Secret reference', `connector_${index}_secret_ref`, stored.connectors?.[index]?.secret_ref ?? connector.secret_ref, 'autocomplete="off"')}<details><summary>Tools, resources and limits</summary><label>Resources · one per line<textarea name="connector_${index}_resources">${esc((connector.resources || []).join('\n'))}</textarea></label><label>Risky tools · one per line<textarea name="connector_${index}_risky_tools">${esc((connector.risky_tools || []).join('\n'))}</textarea></label><label>Denied tools · one per line<textarea name="connector_${index}_denied_tools">${esc((connector.denied_tools || []).join('\n'))}</textarea></label><div class="settings-fields">${Object.entries(connector.limits || {}).map(([name, value]) => settingField(name.replaceAll('_', ' '), `connector_${index}_limit_${name}`, value, 'type="number" min="1" required')).join('')}</div><div class="settings-tool-list">${(connector.tools || []).map((tool, toolIndex) => `<div class="settings-tool-row">${settingField('Tool name', `connector_${index}_tool_${toolIndex}_name`, tool.name, 'required')}<label>Effect<select name="connector_${index}_tool_${toolIndex}_effect"><option value="read_only" ${tool.effect === 'read_only' ? 'selected' : ''}>Read only</option><option value="mutating" ${tool.effect === 'mutating' ? 'selected' : ''}>Mutating</option></select></label><label>Replay<select name="connector_${index}_tool_${toolIndex}_replay"><option value="never" ${tool.replay === 'never' ? 'selected' : ''}>Never</option><option value="safe_with_stable_call_id" ${tool.replay === 'safe_with_stable_call_id' ? 'selected' : ''}>Stable call ID</option></select></label>${settingField('Stable call ID field', `connector_${index}_tool_${toolIndex}_stable`, tool.stable_call_id_field || '')}</div>`).join('')}</div></details></fieldset>`).join('')}${sources.map((source, index) => `<fieldset data-source-index="${index}"><legend>${esc(source.source_id)}</legend><label><input type="checkbox" name="source_${index}_enabled" ${source.enabled ? 'checked' : ''}> Enabled</label>${settingField('Connector ID', `source_${index}_connector_id`, source.connector_id, 'required')}${settingField('List tool', `source_${index}_list_tool`, source.list_tool, 'required')}<label>Mode<select name="source_${index}_mode"><option value="ask" ${source.mode === 'ask' ? 'selected' : ''}>Ask</option><option value="auto" ${source.mode === 'auto' ? 'selected' : ''}>Auto</option></select></label><label>Content<select name="source_${index}_content_type"><option value="structured" ${source.content_type === 'structured' ? 'selected' : ''}>Structured</option><option value="free_text" ${source.content_type === 'free_text' ? 'selected' : ''}>Free text</option></select></label>${settingField('Max pages', `source_${index}_max_pages`, source.max_pages, 'type="number" min="1" max="100" required')}${settingField('Page argument', `source_${index}_page_arg`, source.page_arg || '')}<details><summary>Grouping, observation and writeback</summary>${settingField('Grouping kind', `source_${index}_grouping_kind`, source.grouping?.kind || '')}${settingField('Max in flight', `source_${index}_max_in_flight`, source.grouping?.max_in_flight || '', 'type="number" min="1" max="100"')}${settingField('Max admitted groups', `source_${index}_max_groups`, source.grouping?.max_admitted_groups || '', 'type="number" min="1" max="100"')}${settingField('Observation tool', `source_${index}_observation_tool`, source.observation?.read_tool || '')}${settingField('Observation count path', `source_${index}_count_path`, source.observation?.count_path || '')}${settingField('Observation max age seconds', `source_${index}_max_age`, source.observation?.max_age_s || '', 'type="number" min="1"')}${settingField('Writeback tool', `source_${index}_writeback_tool`, source.writeback?.tool || '')}<label>Writeback event<select name="source_${index}_writeback_on"><option value="">Disabled</option><option value="approved" ${source.writeback?.on === 'approved' ? 'selected' : ''}>Approved</option><option value="closed" ${source.writeback?.on === 'closed' ? 'selected' : ''}>Closed</option></select></label></details></fieldset>`).join('')}</div><div class="settings-safety-note"><b>Restart required</b><p>Apply writes one validated mode-0600 file atomically. It does not enable a connector, run a tool, or restart Butler.</p></div><div class="card-actions"><button class="primary-action" type="submit">Preview source changes</button><button class="button" type="reset">Reset to effective</button></div></form>`;
  }

  function sourceOnboardingEditor(family) {
    if (!family || family.status !== 'configurable') return `<article class="settings-unavailable">${familyStatus(family)}<p>${esc(family?.reason || 'Source onboarding configuration is loading.')}</p></article>`;
    const stored = family.desired || family.effective || {sources: {}};
    const doc = family.effective || stored;
    return `<form class="settings-editor" data-settings-family="source_onboarding" data-expected="${esc(family.expected_sha256)}"><input type="hidden" name="source_document" value="${esc(JSON.stringify(stored))}"><div class="settings-editor-head"><div><h3>Source onboarding</h3><p>Explicit repository mapping, bounded retries and project lifecycle intent.</p></div>${familyStatus(family)}</div><div class="settings-source-grid">${Object.entries(doc.sources || {}).map(([id, source], index) => `<fieldset data-onboarding-index="${index}" data-source-id="${esc(id)}"><legend>${esc(id)}</legend><label>Domain<select name="onboarding_${index}_domain"><option value="work" ${source.domain === 'work' ? 'selected' : ''}>Work</option><option value="personal" ${source.domain === 'personal' ? 'selected' : ''}>Personal</option></select></label><label><input type="checkbox" name="onboarding_${index}_auto" ${source.auto_onboard ? 'checked' : ''}> Auto-onboard mapped repositories</label>${settingField('Projects root', `onboarding_${index}_root`, source.projects_root, 'required')}${settingField('Per-cycle cap', `onboarding_${index}_cap`, source.per_cycle_cap, 'type="number" min="1" max="100" required')}${settingField('Retry limit', `onboarding_${index}_retry`, source.retry_limit, 'type="number" min="1" max="20" required')}${settingField('Retry backoff seconds', `onboarding_${index}_backoff`, source.retry_backoff_s, 'type="number" min="1" max="86400" required')}<label>Repository mappings · hint | HTTPS URL | integration branch<textarea name="onboarding_${index}_repositories">${esc(Object.entries(source.repositories || {}).map(([hint, repo]) => `${hint} | ${repo.repository_url} | ${repo.integration_ref}`).join('\n'))}</textarea></label><label>Member roles · identity=role<textarea name="onboarding_${index}_roles">${esc(Object.entries(source.member_roles || {}).map(([name, role]) => `${name}=${role}`).join('\n'))}</textarea></label><label><input type="checkbox" name="onboarding_${index}_activate" ${source.activate_delivery_policy ? 'checked' : ''}> Request explicit delivery activation</label></fieldset>`).join('')}</div><div class="settings-safety-note"><b>Partial failure recovery</b><p>Applying this file does not clone, create a board, issue credentials, or activate delivery. Guided lifecycle operations remain idempotent and separately confirmed.</p></div><div class="card-actions"><button class="primary-action" type="submit">Preview onboarding changes</button><button class="button" type="reset">Reset to effective</button></div></form>`;
  }

  function renderDispatchPolicy() {
    const data = dispatch.get(keyFor(selectedCentral, selectedBoard));
    if (!data) return '<p class="empty">Dispatch policy is loading.</p>';
    return `<form class="settings-editor" data-settings-family="dispatch"><div class="settings-editor-head"><div><h3>Dispatch timing</h3><p>Concurrent seat capacity is configured separately from offer rates and token budgets.</p></div><span class="status" data-tone="ready">hot apply</span></div><div class="settings-fields">${settingField('Claim lease seconds', 'claim_ttl_s', data.claim_ttl_s, 'type="number" min="1" max="86400" required')}${settingField('Offer seconds', 'offer_ttl_s', data.offer_ttl_s, 'type="number" min="1" max="86400" required')}${settingField('Broadcast re-offer seconds', 'broadcast_reoffer_s', data.broadcast_reoffer_s, 'type="number" min="60" max="86400" required')}<label><input name="second_opinion" type="checkbox" ${data.second_opinion ? 'checked' : ''}> Request second opinion</label><label><input name="fallback_broadcast" type="checkbox" ${data.fallback_broadcast ? 'checked' : ''}> Allow fallback broadcast</label></div><div class="card-actions"><button class="primary-action" type="submit">Save dispatch policy</button></div></form>`;
  }

  function renderDeliverySettings() {
    const rows = deliveries.get(selectedCentral) || [];
    const selected = rows.find(row => row.name === selectedProject) || rows[0];
    if (!selected) return '<p class="empty">No repository delivery settings are available for this coordinator.</p>';
    selectedProject = selected.name;
    const policy = selected.delivery_policy_overrides || {};
    const effective = selected.delivery_policy || {};
    const trigger = policy.release_trigger || {};
    const validation = policy.validation || {};
    const boolean = name => Object.hasOwn(policy, name) ? String(policy[name]) : '';
    return `<form class="settings-editor" data-settings-family="delivery"><div class="settings-editor-head"><div><h3>Delivery policy</h3><p>Global, group and repository overrides remain explicit. Final merge is always manual.</p></div><span class="status" data-tone="${selected.delivery_runtime?.ready === false ? 'warning' : 'ready'}">${selected.delivery_policy_active ? 'active' : selected.delivery_runtime?.ready === false ? 'draft only' : 'ready'}</span></div><div class="settings-fields"><label>Repository<select name="name">${rows.map(row => `<option value="${esc(row.name)}" ${row === selected ? 'selected' : ''}>${esc(row.name)}</option>`).join('')}</select></label><label>Scope<select name="scope"><option value="repository">Repository override</option><option value="group">Named group default</option><option value="global">Global default</option></select></label>${settingField('Named group', 'delivery_policy_group', selected.delivery_policy_group || '', 'pattern="[A-Za-z0-9][A-Za-z0-9._-]{0,79}"')}<label>Mode<select name="mode"><option value="">Inherit</option><option value="per_ticket_pr" ${policy.mode === 'per_ticket_pr' ? 'selected' : ''}>Per-ticket PR</option><option value="batch_pr" ${policy.mode === 'batch_pr' ? 'selected' : ''}>Aggregate PR</option><option value="branch_only" ${policy.mode === 'branch_only' ? 'selected' : ''}>Branch only</option></select></label>${settingField('Mapped base', 'mapped_base', policy.mapped_base || '')}${settingField('Integration branch', 'integration_branch', policy.integration_branch || '')}${settingField('Snapshot prefix', 'snapshot_branch_prefix', policy.snapshot_branch_prefix || '')}${settingField('Final PR target', 'final_pr_target', policy.final_pr_target ?? '')}<label>Release trigger<select name="release_trigger"><option value="">Inherit</option><option value="ready" ${trigger.kind === 'ready' ? 'selected' : ''}>Ready</option><option value="manual" ${trigger.kind === 'manual' ? 'selected' : ''}>Manual</option><option value="scheduled" ${trigger.kind === 'scheduled' ? 'selected' : ''}>Scheduled</option></select></label>${settingField('Timezone', 'timezone', trigger.timezone || '')}${settingField('Schedule', 'schedule', trigger.schedule || '')}<label>PR updates<select name="pr_update"><option value="">Inherit</option><option value="rolling" ${policy.pr_update === 'rolling' ? 'selected' : ''}>Rolling</option><option value="freeze_on_ready" ${policy.pr_update === 'freeze_on_ready' ? 'selected' : ''}>Freeze when ready</option></select></label><label>Automatic integration<select name="auto_integrate"><option value="">Inherit</option><option value="true" ${boolean('auto_integrate') === 'true' ? 'selected' : ''}>Enabled</option><option value="false" ${boolean('auto_integrate') === 'false' ? 'selected' : ''}>Disabled</option></select></label><label>Collection<select name="collection_paused"><option value="">Inherit</option><option value="false" ${boolean('collection_paused') === 'false' ? 'selected' : ''}>Collecting</option><option value="true" ${boolean('collection_paused') === 'true' ? 'selected' : ''}>Paused</option></select></label>${settingField('Required reviewers', 'required_reviewers', validation.required_reviewers || '', 'type="number" min="1" max="10"')}<label>Conflict policy<select name="conflict_policy"><option value="">Inherit</option><option value="pause" ${policy.conflict_policy === 'pause' ? 'selected' : ''}>Pause</option><option value="repair_then_review" ${policy.conflict_policy === 'repair_then_review' ? 'selected' : ''}>Repair then review</option></select></label><label class="settings-wide">Validation commands · one per line<textarea name="test_commands">${esc((validation.test_commands || []).join('\n'))}</textarea></label><label><input name="activate" type="checkbox"> Activate only after installed capability checks pass</label></div><div class="settings-effective"><b>Effective route</b><span>${esc(effective.mapped_base || selected.integration_ref || 'unknown')} → ${esc(effective.integration_branch || 'pursers-integration')} → ${esc(effective.final_pr_target || 'no final PR')}</span><small>Source and inheritance: ${esc(JSON.stringify(selected.delivery_policy_provenance || {}))}</small></div><div class="card-actions"><button class="primary-action" type="submit">Preview delivery changes</button><button class="button" type="button" data-delivery-reset>Reset this scope to inherit</button></div></form>`;
  }

  function renderSeatSetup() {
    const rows = seatInventory?.seats || [];
    const bridge = seatBridge || {};
    return `<div class="settings-editor"><div class="settings-editor-head"><div><h3>Seats, hosts and runner profiles</h3><p>Desired configuration stays distinct from observed Doctor and process state.</p></div><span class="status" data-tone="${bridge.status === 'ready' ? 'ready' : 'warning'}">bridge ${esc(bridge.status || 'loading')}</span></div><div class="table-scroll"><table><thead><tr><th>Seat</th><th>Host / mode</th><th>Role / limits</th><th>Observed</th></tr></thead><tbody>${rows.map(row => `<tr><td><b>${esc(row.name)}</b><br><small>${esc(row.principal_label || 'principal not observed')}</small></td><td>${esc(row.host)} · ${esc(row.host_mode || 'persistent')}<br><small>${esc(row.provider || 'provider default')} / ${esc(row.model || 'model default')}</small></td><td>${esc(row.role)} · tier ${esc(row.tier_max)}<br><small>work ${row.can_work ? 'yes' : 'no'} · review ${row.can_review ? 'yes' : 'no'}</small></td><td>${esc(row.doctor_status || 'not run')}${row.needs_restart ? '<br><span class="restart-badge">Restart required</span>' : ''}</td></tr>`).join('') || '<tr><td colspan="4" class="empty">No configured seats.</td></tr>'}</tbody></table></div><form class="settings-fields settings-seat-form" data-settings-family="seat"><label>Host<select name="host"><option>codex</option><option>codex-cli</option><option>zed</option><option>goose</option><option>claude-code</option><option>claude-desktop</option><option>headless</option></select></label><label>Host lifecycle<select name="host_mode"><option value="persistent">Persistent resident</option><option value="acp">Interactive ACP session</option></select></label><label>Role<select name="role"><option>worker</option><option>reviewer</option><option>orchestrator</option><option>coordinator</option></select></label>${settingField('Fixed seat name', 'name', '', 'pattern="[A-Za-z0-9][A-Za-z0-9._-]{0,79}" required')}${settingField('Registry board', 'registry_board', 'pursers', 'required')}${settingField('Home board', 'home_board', '')}${settingField('Boards', 'boards', 'registry', 'required')}${settingField('Central URL', 'central_url', 'http://127.0.0.1:8766/mcp', 'type="url" required')}${settingField('Token file', 'token_file', '', 'required autocomplete="off"')}${settingField('Token environment variable', 'token_env_var', 'ONBOARD_CENTRAL_TOKEN', 'required')}${settingField('CA file', 'ca_file', '')}${settingField('Bridge command', 'bridge_command', bridge.command || 'pursers-wait-bridge', 'required')}${settingField('Wait connector name', 'bridge_name', '')}${settingField('Board connector name', 'board_connector_name', '')}${settingField('Host config path', 'config_path', '', 'required')}${settingField('Personal command', 'personal_command', 'pursers-personal', 'required')}${settingField('Seat directory', 'seat_dir', '')}${settingField('Repository', 'repository', '')}${settingField('Provider', 'provider', '')}${settingField('Model', 'model', '')}${settingField('Skills', 'skills', '')}<label>Tier max<select name="tier_max"><option>1</option><option selected>2</option><option>3</option></select></label><label><input type="checkbox" name="can_work" checked> Can execute work</label><label><input type="checkbox" name="can_review"> Can review</label><button class="primary-action settings-wide" type="submit">Preview seat and host changes</button></form><div class="card-actions"><button type="button" data-settings-job="doctor">Run Doctor</button><button type="button" data-settings-job="upgrade-all">Plan bridge upgrades</button></div></div>`;
  }

  function visibleSection(title, body) {
    const needle = settingsSearch.trim().toLowerCase();
    return !needle || `${title} ${body}`.toLowerCase().includes(needle);
  }

  function renderManagedSettings() {
    const scope = ensureScope();
    if (!scope) return '<section class="empty-guidance"><h3>No active work project</h3><p>Settings loads only after a registry-backed board is visible.</p></section>';
    const data = managed.get(keyFor(selectedCentral, selectedBoard));
    const sections = [
      ['Projects & sources', 'connector source grouping observation writeback onboarding repository mapping', `<div class="settings-stack">${sourceConnectorEditor(data?.families?.source_connectors)}${sourceOnboardingEditor(data?.families?.source_onboarding)}</div>`],
      ['Delivery', 'branches aggregate per-ticket gates reviewers conflicts schedule handoff', renderDeliverySettings()],
      ['Seats & dispatch', 'capacity concurrent seats rates token budget role host runner profiles IDE', `<div class="settings-stack">${renderSeatSetup()}${renderDispatchPolicy()}${renderMemberships(data)}</div>`],
      ['Board policy & retention', 'review stale archive history journal maintenance release rollback', `<div class="settings-stack">${renderBoardPolicy(data)}${renderRetention(data)}</div>`],
    ];
    const scopeOptions = boardScopes().map(({central, board}) => `<option value="${esc(keyFor(central, board.board_id))}" ${central === selectedCentral && board.board_id === selectedBoard ? 'selected' : ''}>${esc(board.label)} · ${esc(central)} · ${esc(board.board_id)}</option>`).join('');
    return `<section class="settings-control-bar" aria-label="Settings controls"><label>Scope<select data-settings-scope>${scopeOptions}</select></label><label class="settings-search">Search settings<input type="search" data-settings-search value="${esc(settingsSearch)}" placeholder="Search fields and sections"></label><div class="settings-mode" role="group" aria-label="Settings detail"><button type="button" data-settings-mode="simple" aria-pressed="${settingsMode === 'simple'}">Simple</button><button type="button" data-settings-mode="advanced" aria-pressed="${settingsMode === 'advanced'}">Advanced</button></div><button type="button" data-settings-reload>Reload readback</button></section>${settingsNotice ? `<p class="status" role="status">${esc(settingsNotice)}</p>` : ''}${settingsError ? `<p class="error" role="alert">${esc(settingsError)}</p>` : ''}${planPanel()}${loadingKey === keyFor(selectedCentral, selectedBoard) && !data ? '<section class="settings-loading" aria-busy="true"><p>Loading source-backed settings…</p></section>' : ''}<nav class="settings-section-nav" aria-label="Settings sections">${sections.filter(([title, terms]) => visibleSection(title, terms)).map(([title]) => `<a href="#settings-${title.toLowerCase().replaceAll(/[^a-z]+/g, '-')}">${esc(title)}</a>`).join('')}</nav>${sections.filter(([title, terms]) => visibleSection(title, terms)).map(([title, _terms, content], index) => `<section class="settings-section" id="settings-${title.toLowerCase().replaceAll(/[^a-z]+/g, '-')}" data-detail="${index > 1 ? 'advanced' : 'simple'}"><div class="settings-section-head"><div><p class="settings-section-kicker">${index + 1} / ${sections.length}</p><h2>${esc(title)}</h2></div><p>Scope: ${esc(selectedBoard)} · ${esc(selectedCentral)}</p></div>${content}</section>`).join('')}`;
  }

  function replaceButlerData(next) {
    for (const key of Object.keys(butlerData)) delete butlerData[key];
    Object.assign(butlerData, next);
  }

  async function saveButlerSettings(event) {
    event.preventDefault();
    event.stopImmediatePropagation();
    const form = event.target;
    const status = form.querySelector('.butler-result');
    const button = form.querySelector('[data-pursers-action="save-butler"]');
    let extraHeaders;
    try {
      extraHeaders = JSON.parse(form.elements.extra_headers.value.trim() || '{}');
      if (!extraHeaders || Array.isArray(extraHeaders) || typeof extraHeaders !== 'object') {
        throw new Error('must be an object');
      }
    } catch (_error) {
      status.dataset.pursersValidation = 'invalid';
      status.className = 'butler-span butler-result error';
      status.textContent = 'Extra headers must be a JSON object.';
      return;
    }
    const payload = {
      endpoint: form.elements.endpoint.value.trim(),
      model: form.elements.model.value.trim(),
      api_key: form.elements.api_key.value,
      extra_headers: extraHeaders,
      key_header: form.elements.key_header.value.trim(),
      key_prefix: form.elements.key_prefix.value.trim(),
      validation_path: form.elements.validation_path.value.trim(),
      draft_path: form.elements.draft_path.value.trim(),
      draft_protocol: form.elements.draft_protocol.value,
      answering_mode: form.elements.answering_mode.value,
      expected_sha256: butlerData?.expected_sha256 ?? null,
    };
    const central = butlerData?.central || defaultCentral;
    const requestBody = JSON.stringify(payload);
    payload.api_key = '';
    form.elements.api_key.value = '';
    button.disabled = true;
    status.className = 'butler-span butler-result muted';
    status.textContent = 'Validating one bounded provider request…';
    try {
      const response = await fetch(`/api/butler?central=${encodeURIComponent(central)}`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: requestBody,
      });
      let body = {};
      try { body = await response.json(); } catch (_error) {}
      if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
      const next = body.saved === false ? {
        ...body,
        endpoint: payload.endpoint,
        model: payload.model,
        extra_headers: payload.extra_headers,
        key_header: payload.key_header,
        key_prefix: payload.key_prefix,
        validation_path: payload.validation_path,
        draft_path: payload.draft_path,
        draft_protocol: payload.draft_protocol,
        answering_mode: payload.answering_mode,
      } : body;
      replaceButlerData(next);
      renderHub();
    } catch (error) {
      status.dataset.pursersValidation = 'unreachable';
      status.className = 'butler-span butler-result error';
      status.textContent = `Save failed: ${error.message}`;
    } finally {
      button.disabled = false;
    }
  }

  function roleInputs(role, counts = {}, ceiling = 100) {
    return `<fieldset><legend>${esc(role.replace('_', ' '))}</legend><div class="autonomous-fields">${['min', 'target', 'max'].map(name => `<label>${name}<input name="${esc(role)}_${name}" type="number" min="0" max="${esc(ceiling)}" required value="${esc(counts[name] ?? 0)}"></label>`).join('')}</div></fieldset>`;
  }

  function renderAutonomousSettings() {
    const cards = autonomousRows().map(({central, board, data, error, result}) => {
      if (error) {
        return `<article class="card autonomous-card" data-pursers-autonomous-board="${esc(board.board_id)}" data-pursers-state="error"><div class="section-title"><div><p class="eyebrow">${esc(board.label)} · ${esc(central)}</p><h3>Autonomous Butler policy</h3></div><span class="status" data-tone="danger">Unavailable</span></div><p class="error">${esc(error)}</p><p class="settings-next">Next: restore the board connection, then return here to inspect its policy.</p></article>`;
      }
      if (!data) {
        return `<article class="card autonomous-card" aria-busy="true"><p class="eyebrow">${esc(board.label)}</p><h3>Loading autonomous policy…</h3><p class="settings-next">Fleet is reading the current policy and observed runner state.</p></article>`;
      }
      const config = data.config;
      if (!config) {
        return `<article class="card autonomous-card" data-pursers-autonomous-board="${esc(board.board_id)}" data-pursers-state="shadow"><div class="section-title"><div><p class="eyebrow">${esc(board.label)} · ${esc(central)}</p><h3>Autonomous Butler policy</h3></div><span class="status">Shadow</span></div><p class="muted">No autonomous configuration is provisioned. Fleet cannot create envelopes, templates, credentials, or authorization.</p><p class="settings-next">Next: provision policy outside Fleet before attempting to configure this board.</p></article>`;
      }
      const desired = config.desired || {};
      const capacity = desired.capacity || {};
      const cooldowns = desired.cooldowns || {};
      const budget = desired.budget || {};
      const envelope = config.envelope || {};
      const actual = data.actual_state || {};
      const health = new Map((data.actual_state_available ? actual.connectors || [] : []).map(row => [row.connector_id, row]));
      const connectors = desired.connectors || [];
      const state = data.effective_state || 'shadow';
      const autonomous = ['autonomous', 'pending', 'applying', 'degraded'].includes(state);

      return `<article class="card autonomous-card" data-pursers-autonomous-board="${esc(board.board_id)}" data-pursers-state="${esc(state)}"><div class="section-title"><div><p class="eyebrow">${esc(board.label)} · ${esc(central)}</p><h3>Autonomous Butler policy</h3></div><span class="status" data-tone="${state === 'killed' || state === 'auto_demoted' ? 'danger' : autonomous ? 'warning' : 'ready'}">${esc(autonomousStateLabel(state))}</span></div><div class="autonomous-state"><span class="meta">Config revision ${esc(data.revision)}</span><span class="meta" data-autonomous-observation="${esc(data.actual_state_status || 'unavailable')}">Actual observation ${esc(autonomousObservationLabel(data))}</span><span class="meta">Approved templates: ${esc((envelope.approved_template_ids || []).join(', ') || 'none')}</span></div><p class="muted">Autonomous activation is intentionally unavailable here. A separate human-created active authorization must match the envelope and config revision. Saving from Fleet keeps this board in shadow mode and cannot raise immutable ceilings.</p><form class="autonomous-config-form" data-central="${esc(central)}" data-board="${esc(board.board_id)}"><input type="hidden" name="expected_revision" value="${esc(data.revision)}"><fieldset><legend>Mode and runner</legend><div class="autonomous-fields"><label>Mode<select name="mode"><option value="shadow" selected>Shadow</option><option value="autonomous" disabled>Autonomous · separate authorization required</option></select></label><label>Runner<select name="runner"><option value="direct_api" ${desired.runner === 'direct_api' ? 'selected' : ''}>Direct API</option><option value="acp" ${desired.runner === 'acp' ? 'selected' : ''}>ACP worker</option></select></label><label>Host concurrency<input name="host_concurrency" type="number" min="1" max="${esc(envelope.max_host_concurrency || 256)}" required value="${esc(desired.host_concurrency || 1)}"></label><label>Board concurrency<input name="board_concurrency" type="number" min="1" max="${esc(envelope.max_board_concurrency || 256)}" required value="${esc(desired.board_concurrency || 1)}"></label></div></fieldset><div class="autonomous-capacity">${['worker', 'reviewer', 'acp_worker'].map(role => roleInputs(role, capacity[role], envelope.max_capacity?.[role] ?? 100)).join('')}</div><fieldset><legend>Cooldowns and budget</legend><div class="autonomous-fields"><label>Scale up seconds<input name="scale_up_s" type="number" min="0" max="604800" required value="${esc(cooldowns.scale_up_s ?? 0)}"></label><label>Scale down seconds<input name="scale_down_s" type="number" min="0" max="604800" required value="${esc(cooldowns.scale_down_s ?? 0)}"></label><label>Failure backoff seconds<input name="failure_backoff_s" type="number" min="1" max="86400" required value="${esc(cooldowns.failure_backoff_s ?? 1)}"></label><label>Budget period<select name="period">${['hour', 'day', 'month'].map(value => `<option ${budget.period === value ? 'selected' : ''}>${value}</option>`).join('')}</select></label><label>Max tokens<input name="max_tokens" type="number" min="0" max="${esc(envelope.max_budget?.max_tokens ?? 1000000000)}" required value="${esc(budget.max_tokens ?? 0)}"></label><label>Max cost microunits<input name="max_cost_microunits" type="number" min="0" max="${esc(envelope.max_budget?.max_cost_microunits ?? 1000000000000)}" required value="${esc(budget.max_cost_microunits ?? 0)}"></label><label>Max external calls<input name="max_external_calls" type="number" min="0" max="${esc(envelope.max_budget?.max_external_calls ?? 1000000)}" required value="${esc(budget.max_external_calls ?? 0)}"></label></div></fieldset><fieldset><legend>MCP v2 connector allowlists</legend><div class="connector-list">${connectors.map(connector => { const observed = health.get(connector.connector_id) || {}; return `<div class="connector-row"><div><b>${esc(connector.connector_id)}</b><p class="meta">${esc(connector.transport)} · protocol ${esc(connector.protocol_revision)} · health ${esc(observed.status || 'unknown')} · secret ${connector.secret_configured ? 'configured' : 'none'}</p><p class="meta">Tools: ${esc((connector.tools || []).map(tool => tool.name).join(', ') || 'none')} · Resources: ${esc((connector.resources || []).join(', ') || 'none')}</p></div><label><input type="checkbox" name="connector" value="${esc(connector.connector_id)}" ${connector.enabled ? 'checked' : ''}> Enabled</label></div>`; }).join('') || '<p class="empty">No approved connectors.</p>'}</div></fieldset><div class="settings-command-row"><div class="card-actions"><button class="primary-action" type="submit">Save shadow policy</button><button type="button" data-autonomous-command="reconcile_now" data-central="${esc(central)}" data-board="${esc(board.board_id)}">Reconcile now</button><button class="danger-action" type="button" data-autonomous-command="kill" data-central="${esc(central)}" data-board="${esc(board.board_id)}">Kill immediately</button><button type="button" data-autonomous-command="resume" data-central="${esc(central)}" data-board="${esc(board.board_id)}">Human resume</button></div><p class="settings-next">Save validates the revision and remains in shadow. Reconcile requests an immediate observation. Kill and resume send guarded runtime commands.</p></div><p class="autonomous-result ${result ? 'error' : 'muted'}" role="status" aria-live="polite">${esc(result || '')}</p></form></article>`;
    }).join('');

    return `<section class="settings-section" aria-labelledby="settings-autonomy-title"><div class="settings-section-head"><div><p class="settings-section-kicker">Highest risk</p><h2 id="settings-autonomy-title">Automation policy</h2></div><p>Review each board's hard limits before asking a runner to act.</p></div><div class="autonomous-grid">${cards}</div></section>`;
  }

  function renderButlerSettings() {
    if (butlerError) {
      return `<article class="card settings-group butler-settings" data-pursers-panel="butler-settings" data-pursers-state="error"><p class="eyebrow">Board Butler</p><h3>Runtime unavailable</h3><p class="error">${esc(butlerError)}</p><p class="settings-next">Next: check diagnostics for this Central, then reload the current runtime state.</p></article>`;
    }
    if (!butlerData) {
      return `<article class="card settings-group butler-settings" data-pursers-panel="butler-settings" data-pursers-state="loading" aria-busy="true"><p class="eyebrow">Board Butler</p><h3>Loading runtime and provider settings…</h3><p class="settings-next">Fleet is reading configuration; no changes are sent while this loads.</p></article>`;
    }
    const d = butlerData;
    const r = d.runtime || {};
    const validation = d.validation || {};
    const outcome = validation.outcome || 'not_run';
    const labels = {not_configured: 'Not configured', configured_not_running: 'Configured · not running', running_shadow: 'Running · shadow', running_active: 'Running · active'};
    const state = r.state || 'not_configured';
    const activity = r.last_activity_at ? `${relativeAge(r.last_activity_at)} · ${r.last_activity || 'activity'}` : 'No activity observed';
    const tone = state === 'running_active' ? 'danger' : state === 'running_shadow' ? 'ready' : state === 'configured_not_running' ? 'warning' : 'empty';
    const protocol = d.draft_protocol || 'pursers_json_v1';
    const answering = d.answering_mode || 'assist';

    return `<article class="card settings-group butler-settings" data-pursers-panel="butler-settings" data-pursers-state="${esc(state)}" data-pursers-validation="${esc(outcome)}"><div class="section-title"><div><p class="eyebrow">Board Butler · ${esc(d.central || defaultCentral)}</p><h3>Model provider and runtime</h3></div><span class="status" data-tone="${tone}" data-pursers-field="runtime-state">${esc(labels[state] || state)}</span></div><p class="muted" data-pursers-field="last-activity">Last activity: ${esc(activity)}${r.kill_switch_engaged ? ' · kill switch engaged' : ''}</p><div class="settings-safety-note"><b>What changes here</b><p>Validate & save checks the provider, then stores coordinator configuration. The write-only key goes to a private 0600 file and is never returned to this page. Saving does not start Butler. Active answering is accepted only when safe scopes, evidence, ceilings, bounded active windows, and auto-demotion are configured. Saved changes apply on the butler's next question cycle.</p></div><form id="butler-settings-form" class="butler-form"><label class="butler-span">Answering mode<select name="answering_mode" data-pursers-field="answering-mode"><option value="off" ${answering === 'off' ? 'selected' : ''}>Off</option><option value="assist" ${answering === 'assist' ? 'selected' : ''}>Shadow · draft only</option><option value="autonomous" ${answering === 'autonomous' ? 'selected' : ''}>Active · guarded autonomous answers</option></select></label><label class="butler-span">Endpoint URL<input name="endpoint" data-pursers-field="endpoint" type="url" required maxlength="300" placeholder="https://provider.example.invalid/v1" value="${esc(d.endpoint || '')}"></label><label>Model id<input name="model" data-pursers-field="model" required maxlength="200" placeholder="model-id" value="${esc(d.model || '')}"></label><label>API key (write-only)<input name="api_key" data-pursers-field="api-key" type="password" maxlength="8192" autocomplete="new-password" placeholder="${d.key_present ? 'Leave blank to keep existing key' : 'Enter a key if required'}" value=""></label><label>Credential header<input name="key_header" data-pursers-field="key-header" required maxlength="128" value="${esc(d.key_header || 'Authorization')}"></label><label>Credential prefix<input name="key_prefix" data-pursers-field="key-prefix" maxlength="80" value="${esc(d.key_prefix ?? 'Bearer')}"></label><label>Validation path<input name="validation_path" data-pursers-field="validation-path" maxlength="500" value="${esc(d.validation_path || 'models')}"></label><label>Draft path<input name="draft_path" data-pursers-field="draft-path" maxlength="500" value="${esc(d.draft_path || 'draft')}"></label><label class="butler-span">Draft protocol<select name="draft_protocol" data-pursers-field="draft-protocol"><option value="pursers_json_v1" ${protocol === 'pursers_json_v1' ? 'selected' : ''}>Pursers JSON v1</option><option value="openai_chat_completions_v1" ${protocol === 'openai_chat_completions_v1' ? 'selected' : ''}>OpenAI chat completions v1</option></select></label><label class="butler-span">Extra headers (JSON; non-secret only)<textarea name="extra_headers" data-pursers-field="extra-headers">${esc(JSON.stringify(d.extra_headers || {}, null, 2))}</textarea></label><div class="butler-span butler-secret-status" data-pursers-field="key-status"><b>${d.key_present ? 'Key present' : 'No key stored'}</b>${d.key_location ? ` · ${esc(d.key_location)}` : ''}</div><div class="butler-span settings-command-row"><div class="card-actions"><button class="primary-action" type="submit" data-pursers-action="save-butler">Validate & save</button></div><p class="settings-next">Next: review the validation result below. Start and activation remain separate operations.</p></div><p class="butler-span butler-result" role="status" aria-live="polite" data-pursers-validation="${esc(outcome)}">${validation.message ? esc(validation.message) : 'Validation runs once when you save.'}</p></form><div class="settings-danger-zone"><div><b>Emergency stop</b><p class="muted">Stops the resident and leaves the local kill switch engaged. It does not erase provider settings.</p></div><button type="button" data-pursers-action="kill-butler" ${r.running && !r.kill_switch_engaged ? '' : 'disabled'}>Stop Butler now</button></div></article>`;
  }

  function renderDiagnostics() {
    return centralLabels.map(central => `<article class="settings-diagnostic"><div><p class="eyebrow">${esc(central)}</p><h3>Coordinator and diagnostics</h3><p class="muted">Inspect policy, worker controls, and bounded protocol overhead without changing runtime state.</p></div><div class="settings-action-list"><a href="${centralHref(central, 'config')}"><b>Coordinator config</b><span>Read the active coordinator policy and its source.</span></a><a href="${centralHref(central, 'workers')}"><b>Workers</b><span>Inspect local API worker controls and observed state.</span></a><a href="${centralHref(central, 'overhead')}"><b>Overhead</b><span>Review bounded protocol cost and provenance routes.</span></a></div></article>`).join('');
  }

  function renderWarmSettings() {
    return `${pageHead('Settings', 'Configure the system you actually run', 'Search every supported family, inspect effective values and sources, preview redacted changes, then apply with readback.')}${warmTruthStrip()}${renderManagedSettings()}<section class="settings-section" data-detail="advanced" aria-labelledby="settings-butler-title"><div class="settings-section-head"><div><p class="settings-section-kicker">Guarded automation</p><h2 id="settings-butler-title">Butler, autonomy and budgets</h2></div><p>Provider configuration never silently activates a runtime or another model.</p></div><div class="settings-stack">${renderButlerSettings()}${renderAutonomousSettings()}</div></section><section class="settings-section" data-detail="advanced" aria-labelledby="settings-diagnostics-title"><div class="settings-section-head"><div><p class="settings-section-kicker">Diagnostics and maintenance</p><h2 id="settings-diagnostics-title">Inspect before operations</h2></div><p>Release, rollback and maintenance remain guarded, separately confirmed operations.</p></div><div class="settings-diagnostics">${renderDiagnostics()}</div><div class="settings-action-list"><a href="#/seats"><b>Doctor, bridge and release operations</b><span>Run source-backed checks and preview exact operational plans.</span></a><a href="#/team"><b>Observed team readiness</b><span>Compare desired seat configuration with current capabilities.</span></a></div></section>`;
  }

  const renderWarmSettingsBeforeSeatBundle = renderWarmSettings;
  renderWarmSettings = function renderSettingsWithSeatBundle() {
    return renderWarmSettingsBeforeSeatBundle()
      .replace('Prepare a new seat', 'Prepare a cross-board seat')
      .replace('Begin seat setup', 'Generate setup bundle')
      .replace(
        'Fleet opens Seats and preserves the existing plan, diff, and confirmation steps.',
        'Fleet opens Seats so you can copy the secret-free host config, memberships, checklist, and prompt before confirming apply.'
      );
  };

  async function loadSettings(force = false) {
    const scope = ensureScope();
    if (!scope) return;
    const key = keyFor(selectedCentral, selectedBoard);
    if (loadingKey === key || (!force && settledSettings.has(key))) return;
    settledSettings.delete(key);
    loadingKey = key;
    settingsError = '';
    renderHub?.();
    const query = `central=${encodeURIComponent(selectedCentral)}&board_id=${encodeURIComponent(selectedBoard)}`;
    const jobs = [
      api(`/api/config/managed?${query}`).then(value => managed.set(key, value)),
      api(`/api/dispatch?${query}`).then(value => dispatch.set(key, value)),
      api(`/api/projects/delivery?central=${encodeURIComponent(selectedCentral)}`).then(value => deliveries.set(selectedCentral, value.projects || [])),
      api('/api/config/seats').then(value => { seatInventory = value; }),
      api('/api/config/bridge').then(value => { seatBridge = value; }),
    ];
    const results = await Promise.allSettled(jobs);
    const failures = results.filter(result => result.status === 'rejected');
    if (failures.length) settingsError = `${failures.length} settings source${failures.length === 1 ? '' : 's'} unavailable. ${failures.map(result => result.reason.message).join('; ')}`;
    loadingKey = '';
    settledSettings.add(key);
    renderHub?.();
  }

  function nonemptyLines(value) {
    return String(value || '').split('\n').map(item => item.trim()).filter(Boolean);
  }

  function updateSourceDocument(form, family) {
    const documentValue = JSON.parse(form.elements.source_document.value);
    if (family === 'source_connectors') {
      for (const [index, connector] of (documentValue.connectors || []).entries()) {
        connector.enabled = form.elements[`connector_${index}_enabled`].checked;
        connector.transport = form.elements[`connector_${index}_transport`].value;
        connector.protocol_revision = form.elements[`connector_${index}_protocol_revision`].value.trim();
        connector.endpoint_ref = form.elements[`connector_${index}_endpoint_ref`].value.trim();
        const secret = form.elements[`connector_${index}_secret_ref`].value.trim();
        if (secret) connector.secret_ref = secret;
        connector.resources = nonemptyLines(form.elements[`connector_${index}_resources`].value);
        connector.risky_tools = nonemptyLines(form.elements[`connector_${index}_risky_tools`].value);
        connector.denied_tools = nonemptyLines(form.elements[`connector_${index}_denied_tools`].value);
        for (const name of Object.keys(connector.limits || {})) connector.limits[name] = Number(form.elements[`connector_${index}_limit_${name}`].value);
        for (const [toolIndex, tool] of (connector.tools || []).entries()) {
          tool.name = form.elements[`connector_${index}_tool_${toolIndex}_name`].value.trim();
          tool.effect = form.elements[`connector_${index}_tool_${toolIndex}_effect`].value;
          tool.replay = form.elements[`connector_${index}_tool_${toolIndex}_replay`].value;
          const stable = form.elements[`connector_${index}_tool_${toolIndex}_stable`].value.trim();
          if (stable) tool.stable_call_id_field = stable; else delete tool.stable_call_id_field;
        }
      }
      for (const [index, source] of (documentValue.sources || []).entries()) {
        source.enabled = form.elements[`source_${index}_enabled`].checked;
        source.connector_id = form.elements[`source_${index}_connector_id`].value.trim();
        source.list_tool = form.elements[`source_${index}_list_tool`].value.trim();
        source.mode = form.elements[`source_${index}_mode`].value;
        source.content_type = form.elements[`source_${index}_content_type`].value;
        source.max_pages = Number(form.elements[`source_${index}_max_pages`].value);
        const page = form.elements[`source_${index}_page_arg`].value.trim();
        if (page) source.page_arg = page; else delete source.page_arg;
        const grouping = form.elements[`source_${index}_grouping_kind`].value.trim();
        if (grouping) source.grouping = {kind: grouping, max_in_flight: Number(form.elements[`source_${index}_max_in_flight`].value), max_admitted_groups: Number(form.elements[`source_${index}_max_groups`].value)};
        else delete source.grouping;
        const observation = form.elements[`source_${index}_observation_tool`].value.trim();
        if (observation) source.observation = {...(source.observation || {}), read_tool: observation, count_path: form.elements[`source_${index}_count_path`].value.trim(), max_age_s: Number(form.elements[`source_${index}_max_age`].value)};
        else delete source.observation;
        const writeback = form.elements[`source_${index}_writeback_tool`].value.trim();
        if (writeback) source.writeback = {...(source.writeback || {}), tool: writeback, on: form.elements[`source_${index}_writeback_on`].value};
        else delete source.writeback;
      }
    } else {
      const sources = documentValue.sources || {};
      for (const [index, fieldset] of [...form.querySelectorAll('[data-onboarding-index]')].entries()) {
        const source = sources[fieldset.dataset.sourceId];
        source.domain = form.elements[`onboarding_${index}_domain`].value;
        source.auto_onboard = form.elements[`onboarding_${index}_auto`].checked;
        source.projects_root = form.elements[`onboarding_${index}_root`].value.trim();
        source.per_cycle_cap = Number(form.elements[`onboarding_${index}_cap`].value);
        source.retry_limit = Number(form.elements[`onboarding_${index}_retry`].value);
        source.retry_backoff_s = Number(form.elements[`onboarding_${index}_backoff`].value);
        source.activate_delivery_policy = form.elements[`onboarding_${index}_activate`].checked;
        source.repositories = Object.fromEntries(nonemptyLines(form.elements[`onboarding_${index}_repositories`].value).map(line => { const [hint, repository_url, integration_ref = 'main'] = line.split('|').map(value => value.trim()); return [hint, {repository_url, integration_ref}]; }));
        source.member_roles = Object.fromEntries(nonemptyLines(form.elements[`onboarding_${index}_roles`].value).map(line => { const offset = line.indexOf('='); return [line.slice(0, offset).trim(), line.slice(offset + 1).trim()]; }));
      }
    }
    return documentValue;
  }

  async function previewManaged(form) {
    const family = form.dataset.settingsFamily;
    const payload = {board_id: selectedBoard, family, expected_sha256: form.dataset.expected};
    if (family === 'board_policy') payload.changes = {review_policy: form.elements.review_policy.value, stale_after_days: Number(form.elements.stale_after_days.value)};
    else if (family === 'central_retention') payload.changes = Object.fromEntries([...form.querySelectorAll('input[name]')].map(input => [input.name, Number(input.value)]));
    else if (family === 'membership') {
      payload.change = {operation: form.elements.operation.value, principal_id: form.elements.principal_id.value.trim()};
      if (payload.change.operation !== 'remove') payload.change.role = form.elements.role.value;
    } else payload.document = updateSourceDocument(form, family);
    pendingPlan = await api(`/api/config/managed/plan?central=${encodeURIComponent(selectedCentral)}`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
    settingsNotice = 'Preview ready. Review the exact bounded plan before apply.';
    settingsError = '';
    renderHub?.();
  }

  async function saveDispatch(form) {
    const payload = {board_id: selectedBoard, claim_ttl_s: Number(form.elements.claim_ttl_s.value), offer_ttl_s: Number(form.elements.offer_ttl_s.value), broadcast_reoffer_s: Number(form.elements.broadcast_reoffer_s.value), second_opinion: form.elements.second_opinion.checked, fallback_broadcast: form.elements.fallback_broadcast.checked};
    const body = await api(`/api/dispatch?central=${encodeURIComponent(selectedCentral)}`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
    dispatch.set(keyFor(selectedCentral, selectedBoard), body);
    settingsNotice = 'Dispatch policy saved and read back from Central.';
    renderHub?.();
  }

  function deliveryPolicy(form) {
    const policy = {};
    for (const name of ['mode', 'mapped_base', 'integration_branch', 'snapshot_branch_prefix', 'pr_update', 'conflict_policy']) if (form.elements[name].value.trim()) policy[name] = form.elements[name].value.trim();
    if (form.elements.final_pr_target.value.trim()) policy.final_pr_target = form.elements.final_pr_target.value.trim();
    if (form.elements.release_trigger.value) {
      policy.release_trigger = {kind: form.elements.release_trigger.value};
      if (policy.release_trigger.kind === 'scheduled') Object.assign(policy.release_trigger, {timezone: form.elements.timezone.value.trim(), schedule: form.elements.schedule.value.trim()});
    }
    for (const name of ['auto_integrate', 'collection_paused']) if (form.elements[name].value) policy[name] = form.elements[name].value === 'true';
    const commands = nonemptyLines(form.elements.test_commands.value);
    const reviewers = form.elements.required_reviewers.value;
    if (commands.length || reviewers) policy.validation = {test_commands: commands, ...(reviewers ? {required_reviewers: Number(reviewers)} : {}), independent_review: true, require_upstream_policies: true};
    policy.final_merge = 'manual';
    return policy;
  }

  async function previewDelivery(form, reset = false) {
    const payload = {action: 'delivery', scope: form.elements.scope.value, name: form.elements.name.value, delivery_policy_group: form.elements.delivery_policy_group.value.trim() || null, ...(reset ? {reset_to_inherit: true} : {activate: form.elements.scope.value === 'repository' && form.elements.activate.checked, delivery_policy: deliveryPolicy(form)})};
    pendingPlan = await api(`/api/lifecycle/plan?central=${encodeURIComponent(selectedCentral)}`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
    pendingPlan.family = 'delivery';
    settingsNotice = 'Delivery preview ready. Unsupported combinations remain draft-only.';
    renderHub?.();
  }

  async function previewSeat(form) {
    const fields = new FormData(form);
    const payload = Object.fromEntries(fields.entries());
    for (const name of ['can_work', 'can_review']) payload[name] = form.elements[name].checked;
    payload.tier_max = Number(payload.tier_max);
    payload.skills = String(payload.skills || '').split(',').map(value => value.trim()).filter(Boolean);
    for (const name of ['seat_dir', 'repository', 'ca_file', 'home_board', 'bridge_name', 'board_connector_name', 'provider', 'model']) if (!payload[name]) payload[name] = null;
    pendingPlan = await api('/api/config/plan', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
    pendingPlan.family = 'seat';
    settingsNotice = 'Seat plan ready. Host files are not changed until apply.';
    renderHub?.();
  }

  async function applyPending() {
    if (!pendingPlan) return;
    const plan = pendingPlan;
    let receipt;
    if (plan.family === 'delivery') receipt = await api(`/api/lifecycle/apply?central=${encodeURIComponent(selectedCentral)}`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({plan_id: plan.plan_id, plan_digest: plan.plan_digest, confirmation: plan.confirmation})});
    else if (plan.family === 'seat') receipt = await api('/api/config/apply', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({plan_id: plan.plan_id, digest: plan.digest})});
    else receipt = await api(`/api/config/managed/apply?central=${encodeURIComponent(selectedCentral)}`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({plan_id: plan.plan_id, digest: plan.digest})});
    pendingPlan = null;
    settingsNotice = `Applied ${plan.family.replaceAll('_', ' ')}. Readback ${receipt.readback ? 'verified' : 'completed'}${receipt.restart_required ? '; restart required and not performed' : ''}.`;
    await loadSettings(true);
  }

  async function settingsSubmit(event) {
    const form = event.target.closest('[data-settings-family]');
    if (!form) return;
    event.preventDefault();
    settingsError = '';
    try {
      if (form.dataset.settingsFamily === 'dispatch') await saveDispatch(form);
      else if (form.dataset.settingsFamily === 'delivery') await previewDelivery(form);
      else if (form.dataset.settingsFamily === 'seat') await previewSeat(form);
      else await previewManaged(form);
    } catch (error) {
      settingsError = `${form.dataset.settingsFamily.replaceAll('_', ' ')}: ${error.message}`;
      renderHub?.();
    }
  }

  function setSettingsMode(root, mode) {
    settingsMode = mode === 'advanced' ? 'advanced' : 'simple';
    root.querySelectorAll('[data-settings-mode]').forEach(button => {
      button.setAttribute('aria-pressed', String(button.dataset.settingsMode === settingsMode));
    });
    root.querySelectorAll('[data-detail="advanced"]').forEach(section => {
      section.hidden = settingsMode !== 'advanced';
    });
  }

  function bindSettings(context, root) {
    useContext(context);
    root.addEventListener('submit', settingsSubmit);
    root.querySelector('[data-settings-scope]')?.addEventListener('change', event => {
      [selectedCentral, selectedBoard] = event.target.value.split('\u0000');
      pendingPlan = null;
      renderHub?.();
      void loadSettings();
    });
    root.querySelector('[data-settings-search]')?.addEventListener('input', event => { settingsSearch = event.target.value; renderHub?.(); });
    root.querySelectorAll('[data-settings-mode]').forEach(button => button.addEventListener('click', () => {
      setSettingsMode(root, button.dataset.settingsMode);
    }));
    root.querySelector('[data-settings-reload]')?.addEventListener('click', () => void loadSettings(true));
    root.querySelector('[data-settings-cancel]')?.addEventListener('click', () => { pendingPlan = null; settingsNotice = 'Plan cancelled; no changes applied.'; renderHub?.(); });
    root.querySelector('[data-settings-apply]')?.addEventListener('click', () => void applyPending().catch(error => { settingsError = `Apply failed: ${error.message}`; pendingPlan = null; renderHub?.(); }));
    root.querySelector('[data-delivery-reset]')?.addEventListener('click', event => void previewDelivery(event.target.closest('form'), true).catch(error => { settingsError = error.message; renderHub?.(); }));
    root.querySelector('form[data-settings-family="delivery"] select[name="name"]')?.addEventListener('change', event => { selectedProject = event.target.value; renderHub?.(); });
    root.querySelectorAll('[data-settings-job]').forEach(button => button.addEventListener('click', async () => {
      const action = button.dataset.settingsJob;
      const path = action === 'doctor' ? '/api/config/doctor' : '/api/config/bridge/upgrade-all';
      try { await api(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(action === 'doctor' ? {} : {})}); settingsNotice = `${action} job queued. Results remain in the existing operations view.`; renderHub?.(); } catch (error) { settingsError = `${action}: ${error.message}`; renderHub?.(); }
    }));
    setSettingsMode(root, settingsMode);
    const key = keyFor(selectedCentral, selectedBoard);
    root.querySelector('.settings-control-bar')?.setAttribute(
      'data-settings-bound',
      String(settledSettings.has(key) && loadingKey !== key)
    );
    void loadSettings();
  }

  loadStyles();
  if (typeof document !== 'undefined') {
    document.addEventListener('submit', event => {
      if (event.target?.id === 'butler-settings-form') void saveButlerSettings(event);
    }, true);
  }
  globalThis.FleetViewModules.register({
    id: 'settings',
    owns: [
      'seat configuration',
      'dispatch policy',
      'release and door controls'
    ],
    render(context) {
      useContext(context);
      return renderWarmSettings();
    },
    bind(context, root) { bindSettings(context, root); }
  });
})();
