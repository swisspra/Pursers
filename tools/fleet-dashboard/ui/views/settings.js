/* Fleet route module: settings. */
(function registerSettingsView() {
  'use strict';

  let esc, relativeAge, centralLabels, centralHref, pageHead, warmTruthStrip,
    autonomousRows, autonomousStateLabel, autonomousObservationLabel,
    butlerError, butlerData, defaultCentral;

  const STYLE_URL = '/ui/views/settings.css';

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
      butlerError, butlerData, defaultCentral} = context);
  }

  function roleInputs(role, counts = {}, ceiling = 100) {
    return `<fieldset><legend>${esc(role.replace('_', ' '))}</legend><div class="autonomous-fields">${['min', 'target', 'max'].map(name => `<label>${name}<input name="${esc(role)}_${name}" type="number" min="0" max="${esc(ceiling)}" required value="${esc(counts[name] ?? 0)}"></label>`).join('')}</div></fieldset>`;
  }

  function renderAutonomousSettings() {
    const cards = autonomousRows().map(({central, board, data, error}) => {
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

      return `<article class="card autonomous-card" data-pursers-autonomous-board="${esc(board.board_id)}" data-pursers-state="${esc(state)}"><div class="section-title"><div><p class="eyebrow">${esc(board.label)} · ${esc(central)}</p><h3>Autonomous Butler policy</h3></div><span class="status" data-tone="${state === 'killed' || state === 'auto_demoted' ? 'danger' : autonomous ? 'warning' : 'ready'}">${esc(autonomousStateLabel(state))}</span></div><div class="autonomous-state"><span class="meta">Config revision ${esc(data.revision)}</span><span class="meta" data-autonomous-observation="${esc(data.actual_state_status || 'unavailable')}">Actual observation ${esc(autonomousObservationLabel(data))}</span><span class="meta">Approved templates: ${esc((envelope.approved_template_ids || []).join(', ') || 'none')}</span></div><p class="muted">Autonomous activation is intentionally unavailable here. A separate human-created active authorization must match the envelope and config revision. Saving from Fleet keeps this board in shadow mode and cannot raise immutable ceilings.</p><form class="autonomous-config-form" data-central="${esc(central)}" data-board="${esc(board.board_id)}"><input type="hidden" name="expected_revision" value="${esc(data.revision)}"><fieldset><legend>Mode and runner</legend><div class="autonomous-fields"><label>Mode<select name="mode"><option value="shadow" selected>Shadow</option><option value="autonomous" disabled>Autonomous · separate authorization required</option></select></label><label>Runner<select name="runner"><option value="direct_api" ${desired.runner === 'direct_api' ? 'selected' : ''}>Direct API</option><option value="acp" ${desired.runner === 'acp' ? 'selected' : ''}>ACP worker</option></select></label><label>Host concurrency<input name="host_concurrency" type="number" min="1" max="${esc(envelope.max_host_concurrency || 256)}" required value="${esc(desired.host_concurrency || 1)}"></label><label>Board concurrency<input name="board_concurrency" type="number" min="1" max="${esc(envelope.max_board_concurrency || 256)}" required value="${esc(desired.board_concurrency || 1)}"></label></div></fieldset><div class="autonomous-capacity">${['worker', 'reviewer', 'acp_worker'].map(role => roleInputs(role, capacity[role], envelope.max_capacity?.[role] ?? 100)).join('')}</div><fieldset><legend>Cooldowns and budget</legend><div class="autonomous-fields"><label>Scale up seconds<input name="scale_up_s" type="number" min="0" max="604800" required value="${esc(cooldowns.scale_up_s ?? 0)}"></label><label>Scale down seconds<input name="scale_down_s" type="number" min="0" max="604800" required value="${esc(cooldowns.scale_down_s ?? 0)}"></label><label>Failure backoff seconds<input name="failure_backoff_s" type="number" min="1" max="86400" required value="${esc(cooldowns.failure_backoff_s ?? 1)}"></label><label>Budget period<select name="period">${['hour', 'day', 'month'].map(value => `<option ${budget.period === value ? 'selected' : ''}>${value}</option>`).join('')}</select></label><label>Max tokens<input name="max_tokens" type="number" min="0" max="${esc(envelope.max_budget?.max_tokens ?? 1000000000)}" required value="${esc(budget.max_tokens ?? 0)}"></label><label>Max cost microunits<input name="max_cost_microunits" type="number" min="0" max="${esc(envelope.max_budget?.max_cost_microunits ?? 1000000000000)}" required value="${esc(budget.max_cost_microunits ?? 0)}"></label><label>Max external calls<input name="max_external_calls" type="number" min="0" max="${esc(envelope.max_budget?.max_external_calls ?? 1000000)}" required value="${esc(budget.max_external_calls ?? 0)}"></label></div></fieldset><fieldset><legend>MCP v2 connector allowlists</legend><div class="connector-list">${connectors.map(connector => { const observed = health.get(connector.connector_id) || {}; return `<div class="connector-row"><div><b>${esc(connector.connector_id)}</b><p class="meta">${esc(connector.transport)} · protocol ${esc(connector.protocol_revision)} · health ${esc(observed.status || 'unknown')} · secret ${connector.secret_configured ? 'configured' : 'none'}</p><p class="meta">Tools: ${esc((connector.tools || []).map(tool => tool.name).join(', ') || 'none')} · Resources: ${esc((connector.resources || []).join(', ') || 'none')}</p></div><label><input type="checkbox" name="connector" value="${esc(connector.connector_id)}" ${connector.enabled ? 'checked' : ''}> Enabled</label></div>`; }).join('') || '<p class="empty">No approved connectors.</p>'}</div></fieldset><div class="settings-command-row"><div class="card-actions"><button class="primary-action" type="submit">Save shadow policy</button><button type="button" data-autonomous-command="reconcile_now" data-central="${esc(central)}" data-board="${esc(board.board_id)}">Reconcile now</button><button class="danger-action" type="button" data-autonomous-command="kill" data-central="${esc(central)}" data-board="${esc(board.board_id)}">Kill immediately</button><button type="button" data-autonomous-command="resume" data-central="${esc(central)}" data-board="${esc(board.board_id)}">Human resume</button></div><p class="settings-next">Save validates the revision and remains in shadow. Reconcile requests an immediate observation. Kill and resume send guarded runtime commands.</p></div><p class="autonomous-result muted" role="status" aria-live="polite"></p></form></article>`;
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

    return `<article class="card settings-group butler-settings" data-pursers-panel="butler-settings" data-pursers-state="${esc(state)}" data-pursers-validation="${esc(outcome)}"><div class="section-title"><div><p class="eyebrow">Board Butler · ${esc(d.central || defaultCentral)}</p><h3>Model provider and runtime</h3></div><span class="status" data-tone="${tone}" data-pursers-field="runtime-state">${esc(labels[state] || state)}</span></div><p class="muted" data-pursers-field="last-activity">Last activity: ${esc(activity)}${r.kill_switch_engaged ? ' · kill switch engaged' : ''}</p><div class="settings-safety-note"><b>What changes here</b><p>Validate & save checks the provider, then stores coordinator configuration. The write-only key goes to a private 0600 file and is never returned to this page. Saving does not start or activate Butler; changes apply on its next question cycle.</p></div><form id="butler-settings-form" class="butler-form"><label class="butler-span">Endpoint URL<input name="endpoint" data-pursers-field="endpoint" type="url" required maxlength="300" placeholder="https://provider.example.invalid/v1" value="${esc(d.endpoint || '')}"></label><label>Model id<input name="model" data-pursers-field="model" required maxlength="200" placeholder="model-id" value="${esc(d.model || '')}"></label><label>API key (write-only)<input name="api_key" data-pursers-field="api-key" type="password" maxlength="8192" autocomplete="new-password" placeholder="${d.key_present ? 'Leave blank to keep existing key' : 'Enter a key if required'}" value=""></label><label>Credential header<input name="key_header" data-pursers-field="key-header" required maxlength="128" value="${esc(d.key_header || 'Authorization')}"></label><label>Credential prefix<input name="key_prefix" data-pursers-field="key-prefix" maxlength="80" value="${esc(d.key_prefix ?? 'Bearer')}"></label><label>Validation path<input name="validation_path" data-pursers-field="validation-path" maxlength="500" value="${esc(d.validation_path || 'models')}"></label><label>Draft path<input name="draft_path" data-pursers-field="draft-path" maxlength="500" value="${esc(d.draft_path || 'draft')}"></label><label class="butler-span">Draft protocol<select name="draft_protocol" data-pursers-field="draft-protocol"><option value="pursers_json_v1" ${protocol === 'pursers_json_v1' ? 'selected' : ''}>Pursers JSON v1</option><option value="openai_chat_completions_v1" ${protocol === 'openai_chat_completions_v1' ? 'selected' : ''}>OpenAI chat completions v1</option></select></label><label class="butler-span">Extra headers (JSON; non-secret only)<textarea name="extra_headers" data-pursers-field="extra-headers">${esc(JSON.stringify(d.extra_headers || {}, null, 2))}</textarea></label><div class="butler-span butler-secret-status" data-pursers-field="key-status"><b>${d.key_present ? 'Key present' : 'No key stored'}</b>${d.key_location ? ` · ${esc(d.key_location)}` : ''}</div><div class="butler-span settings-command-row"><div class="card-actions"><button class="primary-action" type="submit" data-pursers-action="save-butler">Validate & save</button></div><p class="settings-next">Next: review the validation result below. Start and activation remain separate operations.</p></div><p class="butler-span butler-result" role="status" aria-live="polite" data-pursers-validation="${esc(outcome)}">${validation.message ? esc(validation.message) : 'Validation runs once when you save.'}</p></form><div class="settings-danger-zone"><div><b>Emergency stop</b><p class="muted">Stops the resident and leaves the local kill switch engaged. It does not erase provider settings.</p></div><button type="button" data-pursers-action="kill-butler" ${r.running && !r.kill_switch_engaged ? '' : 'disabled'}>Stop Butler now</button></div></article>`;
  }

  function renderDiagnostics() {
    return centralLabels.map(central => `<article class="settings-diagnostic"><div><p class="eyebrow">${esc(central)}</p><h3>Coordinator and diagnostics</h3><p class="muted">Inspect policy, worker controls, and bounded protocol overhead without changing runtime state.</p></div><div class="settings-action-list"><a href="${centralHref(central, 'config')}"><b>Coordinator config</b><span>Read the active coordinator policy and its source.</span></a><a href="${centralHref(central, 'workers')}"><b>Workers</b><span>Inspect local API worker controls and observed state.</span></a><a href="${centralHref(central, 'overhead')}"><b>Overhead</b><span>Review bounded protocol cost and provenance routes.</span></a></div></article>`).join('');
  }

  function renderWarmSettings() {
    return `${pageHead('Settings', 'Choose the operation, then see its guardrails', 'Start with the outcome you need. Fleet keeps every mutation behind its existing plan, permission, redaction, idempotency, and confirmation checks.')}${warmTruthStrip()}<section class="settings-groups settings-action-map" aria-labelledby="settings-start-title"><div class="settings-intent-list"><div class="settings-section-head"><div><p class="settings-section-kicker">Start here</p><h2 id="settings-start-title">What do you need to change?</h2></div><p>Low-risk inspection comes first. Guarded runtime and release actions stay visibly separate.</p></div><article class="settings-intent" data-risk="standard"><div><p class="eyebrow">Projects and protected doors</p><h3>Connect or maintain work</h3><p>Add projects, inspect registry clones, copy a door once, or prepare a rotation plan without displaying stored secrets.</p></div><div><a class="primary-action" href="#/seats">Open connections</a><p class="settings-next">Next: choose a project or protected door. Any mutation opens its existing plan and confirmation flow.</p></div></article><article class="settings-intent" data-risk="standard"><div><p class="eyebrow">Team and dispatch</p><h3>Maintain an existing seat</h3><p>Inspect seat inventory, run Doctor, upgrade the bridge, or review dispatch policy, offers, and lifecycle controls.</p></div><div class="card-actions"><a class="button" href="#/seats">Seats and dispatch</a><a class="button" href="#/team">Team readiness</a></div><p class="settings-next">Next: select a seat to see observed readiness before choosing a guarded operation.</p></article><article class="settings-intent settings-release" data-risk="high"><div><p class="eyebrow">Release operations</p><h3>Prepare a guarded release plan</h3><p>Stage, publish, kickstart, restart, and registry operations show exact scope before asking for explicit confirmation.</p></div><div><a class="button" href="#/seats">Review operations</a><p class="settings-next">Next: inspect the generated plan and diff. Nothing runs from this link alone.</p></div></article></div><aside class="settings-seat-path" aria-labelledby="settings-seat-title"><div><p class="settings-section-kicker">Guided setup</p><h2 id="settings-seat-title">Prepare a new seat</h2><p>Create the runtime in a deliberate order. Credential generation stays in its current guarded flow.</p></div><ol><li><b>Connect its project</b><span>Confirm the project and protected door are available.</span></li><li><b>Create the seat profile</b><span>Choose its fixed identity, role, tier, and local path.</span></li><li><b>Run Doctor</b><span>Verify the bridge, configuration, and permissions before work starts.</span></li><li><b>Review dispatch</b><span>Confirm offers can reach the seat under the intended policy.</span></li></ol><a class="primary-action" href="#/seats">Begin seat setup</a><p class="settings-next">Next: Fleet opens Seats and preserves the existing plan, diff, and confirmation steps.</p></aside></section><section class="settings-section" aria-labelledby="settings-butler-title"><div class="settings-section-head"><div><p class="settings-section-kicker">Guarded configuration</p><h2 id="settings-butler-title">Butler and model provider</h2></div><p>Configure the provider separately from runtime activation.</p></div>${renderButlerSettings()}</section><section class="settings-section" aria-labelledby="settings-diagnostics-title"><div class="settings-section-head"><div><p class="settings-section-kicker">Read first</p><h2 id="settings-diagnostics-title">Diagnostics by Central</h2></div><p>Inspect source-backed configuration and runtime evidence before changing anything.</p></div><div class="settings-diagnostics">${renderDiagnostics()}</div></section>${renderAutonomousSettings()}`;
  }

  loadStyles();
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
    }
  });
})();
