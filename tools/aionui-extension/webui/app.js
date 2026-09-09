'use strict';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const helperForm = $('#helper-form');
const helperUrlInput = $('#helper-url');
const helperTokenInput = $('#helper-token');
const connectionForm = $('#connection-form');
const doorInput = $('#door');
const connectionMessage = $('#connection-message');
const connectionCard = $('#connection-card');
const teamForm = $('#team-form');
const teamMessage = $('#team-message');
const seatList = $('#seat-list');
const seatTemplate = $('#seat-template');
const planCard = $('#plan-card');
const planResults = $('#plan-results');
const confirmStart = $('#confirm-start');
const startTeam = $('#start-team');
const rosterList = $('#roster-list');
const rosterEmpty = $('#roster-empty');
const submissionList = $('#submission-list');
const resultsEmpty = $('#results-empty');
const resultState = $('#result-state');
const stopDialog = $('#stop-dialog');
const ticketForm = $('#ticket-form');
const ticketMessage = $('#ticket-message');
const ticketList = $('#ticket-list');
const ticketEmpty = $('#ticket-empty');

const state = {
  helper: null,
  connection: null,
  teamAvailable: false,
  teamReady: false,
  plannedSpec: null,
  planCanApply: false,
  pendingStop: null,
};

const ERROR_COPY = {
  helper_auth_failed: 'The helper rejected this local access token.',
  origin_not_allowed: 'The helper is not bound to this exact AionCore origin.',
  bridge_not_installed: 'The local Pursers bridge is not installed. Install it, then retry.',
  bridge_status_failed: 'Saved connection status is unavailable. Check the bridge and retry.',
  expired_door: 'This door has expired. Ask your coordinator for a replacement.',
  rotation_required: 'A different door is already stored. Use Replace door to continue safely.',
  rotation_requires_existing: 'Connect the original board and role before replacing its door.',
  mcp_registration_failed: 'The seat connected, but AionUi registration did not finish. Use Recover registration.',
  identity_conflict: 'That seat name belongs to a different identity. Choose a unique seat name.',
  server_unreachable: 'The project service is unreachable. Nothing changed; check connectivity and retry.',
  wrong_board: 'This door belongs to a different project.',
  wrong_role: 'This door was issued for a different role.',
  invalid_door: 'This door could not be read. Ask your coordinator for a valid replacement.',
  runtime_context_missing: 'This settings page has no Team conversation runtime. Open or create the Team in AionUi and use its native controls.',
  runtime_auth_failed: 'AionUi could not authorize this Team context. Nothing changed.',
  permission_denied: 'This action requires the Team lead. Nothing changed.',
  not_in_team: 'This conversation is not part of an AionUi Team.',
  transport_unavailable: 'The authenticated local helper is unavailable. Nothing changed.',
  backend_unavailable: 'Board data is unavailable. Start the local Fleet dashboard and retry. If ticket actions also fail, restart or reconnect the local helper.',
  invalid_backend_response: 'The board result source returned an invalid response. Nothing was displayed.',
  invalid_result_state: 'Choose one of the available result states.',
  ticket_not_found: 'That ticket is not present in the bounded board response. Refresh and retry.',
  invalid_response: 'The local helper returned an invalid response. Nothing was displayed.',
  schema_validation_failed: 'Check the highlighted Team fields and retry.',
  invalid_json: 'The local host rejected an invalid request. Reload and retry.',
  not_connected: 'Connect a door for this board, then retry.',
  board_mismatch: 'This helper is pinned to a different board.',
  conflict: 'The ticket changed. Refresh its persisted state before retrying.',
  invalid_input: 'Correct the ticket fields and retry.',
};

const HELPER_TOKEN_HEADER = 'x-pursers-home-token';

function errorCode(result) {
  return result.code || result.error?.code || result.error || 'unknown_error';
}

function messageFor(result, fallback) {
  const code = errorCode(result);
  return ERROR_COPY[code] || result.message || result.error?.message || fallback;
}

async function api(path, options = {}) {
  if (!state.helper) {
    return { response: null, result: { ok: false, error: 'transport_unavailable' } };
  }
  const request = { ...options, headers: { ...(options.headers || {}) } };
  if (Object.prototype.hasOwnProperty.call(request, 'json')) {
    request.method ||= 'POST';
    request.headers['content-type'] = 'application/json';
    request.body = JSON.stringify(request.json);
    delete request.json;
  }
  request.headers[HELPER_TOKEN_HEADER] = state.helper.token;
  request.credentials = 'omit';
  request.cache = 'no-store';
  request.referrerPolicy = 'no-referrer';
  try {
    const response = await fetch(`${state.helper.baseUrl}${path}`, request);
    let result;
    try {
      result = await response.json();
    } catch (_error) {
      result = { ok: false, error: 'invalid_response' };
    }
    return { response, result };
  } catch (_error) {
    return { response: null, result: { ok: false, error: 'transport_unavailable' } };
  }
}

function loopbackHelperOrigin(value) {
  let url;
  try {
    url = new URL(value);
  } catch (_error) {
    return null;
  }
  const host = url.hostname.replace(/^\[|\]$/g, '').toLowerCase();
  const ipv4 = host.split('.').map(Number);
  const loopback = host === 'localhost'
    || host.endsWith('.localhost')
    || host === '::1'
    || (ipv4.length === 4 && ipv4[0] === 127 && ipv4.every((part) => Number.isInteger(part) && part >= 0 && part <= 255));
  if (url.protocol !== 'http:' || !loopback || url.username || url.password || url.pathname !== '/' || url.search || url.hash) return null;
  return url.origin;
}

function showHelper(status) {
  for (const field of ['board', 'central', 'transport', 'core_version', 'team_context']) {
    $(`[data-helper-field="${field}"]`).textContent = String(status[field] || '—').replaceAll('_', ' ');
  }
  setPill($('#helper-pill'), `Connected · ${status.board}`, 'ready');
  setPill($('#helper-state'), 'Authenticated', 'ready');
}

async function connectHelper(event) {
  event.preventDefault();
  if (!helperForm.reportValidity()) return;
  const baseUrl = loopbackHelperOrigin(helperUrlInput.value.trim());
  const token = helperTokenInput.value;
  helperTokenInput.value = '';
  if (!baseUrl) {
    setMessage($('#helper-message'), 'Use the exact HTTP loopback origin printed by the helper.', 'error');
    return;
  }
  const button = $('#connect-helper');
  setBusy(button, true, 'Connecting…');
  state.helper = { baseUrl, token };
  const { response, result } = await api('/pursers/helper/status');
  setBusy(button, false);
  if (!response?.ok || !result.ok) {
    state.helper = null;
    const text = messageFor(result, 'The local helper is unavailable.');
    setMessage($('#helper-message'), text, 'error');
    setPill($('#helper-pill'), 'Not connected', 'error');
    setPill($('#helper-state'), 'Unavailable', 'warning');
    showGlobal('Local helper needs attention', text, 'warning');
    return;
  }
  state.helper.board = result.board;
  showHelper(result);
  setMessage($('#helper-message'), `Connected to the helper for board ${result.board}.`, 'success');
  showGlobal('Local helper connected', `Pursers Home is bound to board ${result.board}. Refreshing redacted status.`, 'info');
  await Promise.all([loadConnection(), loadTeamStatus(), loadTickets(), loadResults()]);
}

async function importMcpDefinition(result) {
  if (result.imported !== false) return true;
  if (!result.mcp_definition) return false;
  try {
    const headers = { 'content-type': 'application/json' };
    const csrfCookie = document.cookie
      .split(';')
      .map((part) => part.trim())
      .find((part) => part.startsWith('aionui-csrf-token='));
    if (csrfCookie) {
      headers['x-csrf-token'] = csrfCookie.slice('aionui-csrf-token='.length);
    }
    const response = await fetch('/api/mcp/servers/import', {
      method: 'POST',
      credentials: 'same-origin',
      headers,
      body: JSON.stringify({
        servers: [{ ...result.mcp_definition, builtin: false, enabled: true }],
      }),
    });
    const body = await response.json();
    return response.ok && body.success !== false;
  } catch (_error) {
    return false;
  }
}

function setMessage(target, text, tone = '') {
  target.textContent = text;
  target.classList.remove('error', 'success');
  if (tone) target.classList.add(tone);
}

function setPill(target, text, tone = 'neutral') {
  target.textContent = text;
  target.className = `status-pill ${tone}`;
}

function setBusy(button, busy, busyText) {
  if (!button.dataset.label) button.dataset.label = button.textContent;
  button.disabled = busy;
  button.textContent = busy ? busyText : button.dataset.label;
}

function showGlobal(title, detail, tone = 'info') {
  const notice = $('#global-notice');
  notice.className = `notice notice-${tone}`;
  $('strong', notice).textContent = title;
  $('p', notice).textContent = detail;
}

function updateJourney() {
  const connected = Boolean(state.connection);
  const ready = state.teamReady;
  const nextTitle = $('#next-step-title');
  const nextCopy = $('#next-step-copy');
  const nextAction = $('#next-step-action');
  const steps = Object.fromEntries($$('.stepper li').map((item) => [item.dataset.step, item]));
  Object.values(steps).forEach((item) => item.classList.remove('current', 'complete'));
  if (!state.helper) {
    nextTitle.textContent = 'Connect the local helper';
    nextCopy.textContent = 'Use the exact loopback URL and local access token printed by the packaged helper.';
    nextAction.textContent = 'Connect helper';
    nextAction.href = '#helper';
    steps.connection.classList.add('current');
  } else if (!connected) {
    nextTitle.textContent = 'Connect your first project';
    nextCopy.textContent = 'Use the door supplied by your coordinator. It is sent directly to the protected local backend and cleared from this page.';
    nextAction.textContent = 'Connect project';
    nextAction.href = '#connection';
    steps.connection.classList.add('current');
  } else if (!ready) {
    nextTitle.textContent = 'Prepare the Team for this project';
    nextCopy.textContent = 'Review distinct teammate identities, workspace folders, roles, and tier ceilings before starting anyone.';
    nextAction.textContent = 'Prepare Team';
    nextAction.href = '#team';
    steps.connection.classList.add('complete');
    steps.team.classList.add('current');
  } else {
    nextTitle.textContent = 'Your Team is ready';
    nextCopy.textContent = 'Board dispatch assigns work. Open the Pursers Personal app to follow progress and reviewed results.';
    nextAction.textContent = 'View Team status';
    nextAction.href = '#progress';
    steps.connection.classList.add('complete');
    steps.team.classList.add('complete');
    steps.ready.classList.add('current');
  }
}

function formatExpiry(value) {
  const epoch = Number(value);
  return Number.isFinite(epoch) && epoch > 0
    ? new Date(epoch * 1000).toLocaleString()
    : '—';
}

function showConnection(status) {
  if (!status) return;
  const seatNames = Array.isArray(status.seat_names) ? status.seat_names : [];
  const normalized = {
    board: status.board,
    role: status.role,
    seat_name: status.seat_name || seatNames.join(', ') || 'Reserved on first session',
    push_mode: status.push_mode,
    kid: status.kid,
    exp: status.exp,
  };
  state.connection = normalized;
  for (const field of ['board', 'role', 'seat_name', 'push_mode', 'kid', 'exp']) {
    const target = `[data-field="${field}"]`;
    const value = field === 'exp' ? formatExpiry(normalized[field]) : (normalized[field] || '—');
    $(target, connectionCard).textContent = String(value);
  }
  $('#seat-name').value = normalized.seat_name.includes(',') ? '' : normalized.seat_name;
  $('#role').value = normalized.role || 'worker';
  $('#seat-folder').value = $('#seat-name').value || 'pursers-seat';
  setPill($('#saved-state'), 'Connected', 'ready');
  setPill($('#connection-pill'), 'Connected', 'ready');
  $('#recover-seat').disabled = normalized.seat_name.includes(',') || normalized.seat_name.startsWith('Reserved');
  $('#sidebar-state').textContent = 'Project connected';
  $('#sidebar-dot').className = 'dot dot-ready';
  updateJourney();
}

function connectionPayload(door) {
  const payload = {
    door,
    seat_name: $('#seat-name').value.trim(),
    expected_role: $('#role').value,
    tier_max: Number($('#tier-max').value),
    folder: $('#seat-folder').value.trim(),
  };
  const assistant = $('#assistant-id').value.trim();
  const model = $('#model').value.trim();
  if (assistant) payload.assistant_id = assistant;
  if (model) payload.model = model;
  return payload;
}

async function submitDoor(operation, button) {
  if (!connectionForm.reportValidity()) return;
  const door = doorInput.value.trim();
  doorInput.value = '';
  if (!door) {
    setMessage(connectionMessage, 'Paste a door before continuing.', 'error');
    return;
  }
  setBusy(button, true, operation === 'validate' ? 'Checking…' : 'Connecting…');
  setMessage(connectionMessage, 'The door is being checked locally.');
  const { response, result } = await api(`/pursers/onboarding/${operation}`, {
    json: connectionPayload(door),
  });
  setBusy(button, false);
  if (!response?.ok || !result.ok) {
    const text = messageFor(result, 'The connection could not be completed. Nothing changed.');
    setMessage(connectionMessage, text, 'error');
    setPill($('#connection-pill'), 'Needs attention', 'error');
    if (result.connected) $('#recover-seat').disabled = false;
    showGlobal('Connection needs attention', text, 'error');
    return;
  }
  if (operation === 'validate') {
    const metadata = result.metadata || {};
    setMessage(connectionMessage, `Door ready for ${metadata.board || 'this project'} · ${metadata.role || 'seat'} · expires ${formatExpiry(metadata.exp)}.`, 'success');
    setPill($('#connection-pill'), 'Door ready', 'ready');
    showGlobal('Door is valid', 'Review the project role, paste the same door again, then select Connect project. The checked value was cleared from the page.', 'info');
    return;
  }
  showConnection(result.status);
  if (!(await importMcpDefinition(result))) {
    setMessage(connectionMessage, 'Project connected, but AionUi MCP registration needs attention. Select Recover registration after restoring host access.', 'error');
    $('#recover-seat').disabled = false;
    showGlobal('Registration needs attention', 'The local door is stored, but AionUi rejected same-origin MCP registration. No credential was sent to the helper.', 'warning');
    return;
  }
  const outcome = result.outcome === 'rotated' ? 'Connection replaced' : 'Project connected';
  setMessage(connectionMessage, `${outcome}. AionUi registration is ready.`, 'success');
  showGlobal(outcome, 'The saved status is redacted. Next, prepare distinct Team seats and preview the plan.', 'info');
  if (result.team?.seat) addSeat(result.team.seat, true);
}

async function loadConnection() {
  const { response, result } = await api('/pursers/onboarding/status');
  if (!response?.ok || !result.ok) {
    const text = messageFor(result, 'No saved project connection was found.');
    setPill($('#connection-pill'), 'Not connected', 'neutral');
    setPill($('#saved-state'), 'Unavailable', 'warning');
    $('#sidebar-state').textContent = 'Project not connected';
    $('#sidebar-dot').className = 'dot dot-warning';
    showGlobal('Connect a project to begin', text, result.code === 'bridge_not_installed' ? 'warning' : 'info');
    updateJourney();
    return;
  }
  const first = Array.isArray(result.seats) ? result.seats[0] : null;
  if (!first) {
    setPill($('#connection-pill'), 'Not connected', 'neutral');
    setPill($('#saved-state'), 'No saved seat', 'neutral');
    showGlobal('Connect a project to begin', 'Paste one coordinator-issued door. No manual configuration file is required.', 'info');
    updateJourney();
    return;
  }
  showConnection({ ...first, push_mode: result.push_mode });
  showGlobal('Project connection is ready', 'Review the saved redacted status, then prepare or refresh the Team.', 'info');
}

async function recoverConnection() {
  if (!state.connection) return;
  const button = $('#recover-seat');
  setBusy(button, true, 'Recovering…');
  const payload = {
    board: state.connection.board,
    role: state.connection.role,
    seat_name: state.connection.seat_name,
    tier_max: Number($('#tier-max').value),
    folder: $('#seat-folder').value.trim(),
  };
  const assistant = $('#assistant-id').value.trim();
  const model = $('#model').value.trim();
  if (assistant) payload.assistant_id = assistant;
  if (model) payload.model = model;
  const { response, result } = await api('/pursers/onboarding/recover', { json: payload });
  setBusy(button, false);
  if (!response?.ok || !result.ok) {
    setMessage(connectionMessage, messageFor(result, 'Recovery failed. Nothing changed.'), 'error');
    return;
  }
  showConnection(result.status);
  if (await importMcpDefinition(result)) {
    setMessage(connectionMessage, 'Registration recovered without replaying the door.', 'success');
  } else {
    setMessage(connectionMessage, 'The saved project is available, but AionUi still rejected MCP registration.', 'error');
  }
}

function addSeat(seed = {}, replaceMatch = false) {
  if (replaceMatch && seed.name) {
    const match = $$('.seat-row').find((row) => $('[data-seat-field="name"]', row).value === seed.name);
    if (match) return;
  }
  const fragment = seatTemplate.content.cloneNode(true);
  const row = $('.seat-row', fragment);
  const index = $$('.seat-row').length + 1;
  $('.seat-number', row).textContent = String(index);
  const defaults = {
    name: seed.name || `pursers-${index === 1 ? 'worker' : 'reviewer'}-${index}`,
    assistant_id: seed.assistant_id || '',
    role: seed.role || (index === 1 ? 'worker' : 'reviewer'),
    tier_max: seed.tier_max || 2,
    folder: seed.folder || `pursers-${index === 1 ? 'worker' : 'reviewer'}-${index}`,
    model: seed.model || '',
  };
  for (const [field, value] of Object.entries(defaults)) {
    $(`[data-seat-field="${field}"]`, row).value = String(value);
  }
  $('.remove-seat', row).addEventListener('click', () => {
    row.remove();
    $$('.seat-row').forEach((item, position) => {
      $('.seat-number', item).textContent = String(position + 1);
    });
    resetPlan();
  });
  $$('input, select', row).forEach((input) => input.addEventListener('input', resetPlan));
  resetPlan();
  seatList.append(fragment);
}

function buildTeamSpec(live = false) {
  const seats = $$('.seat-row').map((row) => {
    const seat = {
      name: $('[data-seat-field="name"]', row).value.trim(),
      assistant_id: $('[data-seat-field="assistant_id"]', row).value.trim(),
      role: $('[data-seat-field="role"]', row).value,
      tier_max: Number($('[data-seat-field="tier_max"]', row).value),
      folder: $('[data-seat-field="folder"]', row).value.trim(),
    };
    const model = $('[data-seat-field="model"]', row).value.trim();
    if (model) seat.model = model;
    return seat;
  });
  if (!seats.some((seat) => seat.role === 'worker') || !seats.some((seat) => seat.role === 'reviewer')) {
    throw new Error('Add at least one worker and one independent reviewer.');
  }
  return {
    team: { name: $('#team-name').value.trim() },
    lead: {
      name: $('#lead-name').value.trim(),
      assistant_id: $('#lead-assistant').value.trim(),
      monitor_only: true,
    },
    seats,
    options: live
      ? { confirm: 'apply-live', dry_run: false, send_kickoff: true }
      : { dry_run: true, send_kickoff: true },
  };
}

function resetPlan() {
  state.plannedSpec = null;
  state.planCanApply = false;
  confirmStart.checked = false;
  confirmStart.disabled = false;
  startTeam.disabled = true;
  planCard.hidden = true;
}

function resultTone(outcome) {
  if (['ok', 'connected', 'recovered', 'rotated', 'skipped_exists'].includes(outcome)) return 'ready';
  if (['dry_run', 'unsupported_by_host'].includes(outcome)) return 'warning';
  return 'error';
}

function renderResults(container, result) {
  container.replaceChildren();
  for (const item of result.results || []) {
    const row = document.createElement('article');
    row.className = 'result-row';
    const name = document.createElement('strong');
    name.textContent = item.seat || item.slot_id || 'Team';
    const action = document.createElement('span');
    action.className = 'status-pill neutral';
    action.textContent = item.action || result.op || 'status';
    const detail = document.createElement('p');
    detail.textContent = item.detail || 'No detail returned.';
    const outcome = document.createElement('span');
    setPill(outcome, item.outcome || 'unknown', resultTone(item.outcome));
    row.append(name, action, detail, outcome);
    container.append(row);
  }
}

async function planTeam(event) {
  event.preventDefault();
  if (!teamForm.reportValidity()) return;
  let spec;
  try {
    spec = buildTeamSpec(false);
  } catch (error) {
    setMessage(teamMessage, error.message, 'error');
    return;
  }
  const button = $('#plan-team');
  setBusy(button, true, 'Planning…');
  const { response, result } = await api('/pursers/team/plan', { json: spec });
  setBusy(button, false);
  if (!response?.ok || !result.ok) {
    const text = messageFor(result, 'The Team plan could not be read. Nothing changed.');
    if (errorCode(result) === 'runtime_context_missing') {
      renderResults(planResults, {
        results: [{
          seat: 'AionUi Team',
          action: 'Open native Team',
          outcome: 'unsupported_by_host',
          detail: 'This settings page has no conversation runtime. Create or open the Team in AionUi and use its native controls.',
        }],
      });
      planCard.hidden = false;
      state.plannedSpec = null;
      state.planCanApply = false;
      confirmStart.checked = false;
      confirmStart.disabled = true;
      startTeam.disabled = true;
      setPill($('#plan-state'), 'Use native Team', 'warning');
      setMessage(teamMessage, text, 'error');
      setPill($('#team-pill'), 'Native Team required', 'warning');
      showGlobal('Use AionUi Team controls', text, 'warning');
      return;
    }
    setMessage(teamMessage, text, 'error');
    setPill($('#team-pill'), 'Unavailable', 'warning');
    showGlobal('Team context is not ready', text, 'warning');
    return;
  }
  renderResults(planResults, result);
  planCard.hidden = false;
  state.plannedSpec = spec;
  state.planCanApply = (result.results || []).every((item) => !['failed', 'identity_conflict', 'unsupported_by_host'].includes(item.outcome));
  setPill($('#plan-state'), state.planCanApply ? 'Safe to confirm' : 'Resolve conflicts', state.planCanApply ? 'ready' : 'error');
  setMessage(teamMessage, state.planCanApply ? 'Dry-run plan ready. No teammate was changed.' : 'The dry-run found conflicts. Resolve them before starting.', state.planCanApply ? 'success' : 'error');
  confirmStart.checked = false;
  confirmStart.disabled = !state.planCanApply;
  startTeam.disabled = true;
  planCard.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

async function applyTeam() {
  if (!state.plannedSpec || !state.planCanApply || !confirmStart.checked) return;
  const button = startTeam;
  setBusy(button, true, 'Starting…');
  const liveSpec = {
    ...state.plannedSpec,
    team: { ...state.plannedSpec.team },
    lead: { ...state.plannedSpec.lead },
    seats: state.plannedSpec.seats.map((seat) => ({ ...seat })),
    options: { confirm: 'apply-live', dry_run: false, send_kickoff: true },
  };
  const { response, result } = await api('/pursers/team/apply', { json: liveSpec });
  setBusy(button, false);
  confirmStart.checked = false;
  startTeam.disabled = true;
  if (!response?.ok || !result.ok) {
    const text = messageFor(result, 'The Team could not be started. Nothing else was changed.');
    setMessage(teamMessage, text, 'error');
    showGlobal('Team start needs attention', text, 'error');
    return;
  }
  renderResults(planResults, result);
  const failed = Number(result.summary?.failed || 0) + Number(result.summary?.identity_conflict || 0);
  const success = Number(result.summary?.ok || 0) + Number(result.summary?.skipped_exists || 0);
  const text = failed
    ? `${success} Team actions succeeded; ${failed} need attention. Work has not started automatically.`
    : `${success} Team actions completed. Board dispatch remains in control of work.`;
  setMessage(teamMessage, text, failed ? 'error' : 'success');
  showGlobal(failed ? 'Team partially started' : 'Team is prepared', text, failed ? 'warning' : 'info');
  await loadTeamStatus();
}

function makeRosterRow(member) {
  const row = document.createElement('article');
  row.className = 'roster-row';
  const name = document.createElement('strong');
  name.textContent = member.name || 'Unnamed teammate';
  const status = document.createElement('span');
  setPill(status, member.status || 'unknown', member.status === 'working' || member.status === 'idle' ? 'ready' : 'warning');
  const detail = document.createElement('p');
  detail.textContent = [member.role, member.assistant_id, member.model].filter(Boolean).join(' · ') || 'No role details returned.';
  const actions = document.createElement('div');
  actions.className = 'row-actions';
  if (member.role !== 'lead' && member.slot_id) {
    const pause = document.createElement('button');
    pause.type = 'button';
    pause.className = 'button button-secondary';
    pause.textContent = 'Pause safely';
    pause.addEventListener('click', () => pauseSeat(member.slot_id, pause));
    const stop = document.createElement('button');
    stop.type = 'button';
    stop.className = 'button button-danger-quiet';
    stop.textContent = 'Stop';
    stop.addEventListener('click', () => openStop(member));
    actions.append(pause, stop);
  }
  row.append(name, status, detail, actions);
  return row;
}

async function loadTeamStatus() {
  const { response, result } = await api('/pursers/team/status');
  if (!response?.ok || !result.ok) {
    state.teamAvailable = false;
    state.teamReady = false;
    rosterList.replaceChildren();
    rosterEmpty.hidden = false;
    setPill($('#team-pill'), 'Context unavailable', 'warning');
    $('#team-context-notice').hidden = false;
    updateJourney();
    return result;
  }
  state.teamAvailable = true;
  const members = Array.isArray(result.members) ? result.members : [];
  state.teamReady = members.some((member) => member.role === 'teammate');
  rosterList.replaceChildren(...members.map(makeRosterRow));
  rosterEmpty.hidden = members.length > 0;
  $('#team-context-notice').hidden = true;
  setPill($('#team-pill'), members.length ? `${members.length} members` : 'Team is empty', members.length ? 'ready' : 'warning');
  updateJourney();
  return result;
}

const RESULT_LABELS = {
  missing: 'Missing submission',
  pending: 'Pending review',
  approved: 'Approved',
  rejected: 'Rejected',
  failed: 'Failed',
};

function makeResultRow(item) {
  const article = document.createElement('article');
  article.className = 'submission-row';
  const heading = document.createElement('div');
  heading.className = 'submission-heading';
  const identity = document.createElement('div');
  const ticket = document.createElement('strong');
  ticket.textContent = `${item.ticket_id} · ${item.title}`;
  const metadata = document.createElement('p');
  metadata.textContent = item.submitted_at ? `Submitted ${item.submitted_at}` : 'No submitted work is available.';
  identity.append(ticket, metadata);
  const pill = document.createElement('span');
  pill.className = `status-pill ${item.result_state}`;
  pill.textContent = RESULT_LABELS[item.result_state] || item.result_state;
  heading.append(identity, pill);
  article.append(heading);

  if (item.submission?.summary) {
    const summary = document.createElement('p');
    summary.className = 'submission-summary';
    summary.textContent = item.submission.summary;
    article.append(summary);
  }
  if (item.submission?.branch && item.submission?.commit) {
    const revision = document.createElement('p');
    revision.className = 'submission-revision';
    const label = document.createElement('span');
    label.textContent = 'Revision ';
    const code = document.createElement('code');
    code.textContent = `${item.submission.branch} @ ${item.submission.commit}`;
    revision.append(label, code);
    article.append(revision);
  }
  const files = Array.isArray(item.submission?.files_changed) ? item.submission.files_changed : [];
  if (files.length) {
    const label = document.createElement('p');
    label.className = 'artifact-label';
    label.textContent = `Artifact references (${files.length})`;
    const list = document.createElement('ul');
    list.className = 'artifact-list';
    for (const file of files) {
      const row = document.createElement('li');
      const code = document.createElement('code');
      code.textContent = file;
      row.append(code);
      list.append(row);
    }
    if (item.submission.files_omitted > 0) {
      const row = document.createElement('li');
      row.textContent = `${item.submission.files_omitted} additional references omitted by the bounded response.`;
      list.append(row);
    }
    article.append(label, list);
  }
  const review = document.createElement('p');
  review.className = 'review-outcome';
  if (item.review?.verdict) {
    const reviewer = item.review.reviewer || 'unnamed reviewer';
    const independence = item.review.independent ? 'independent review' : 'independence not established';
    review.textContent = `Review: ${item.review.verdict} by ${reviewer} · ${independence}${item.review.reviewed_at ? ` · ${item.review.reviewed_at}` : ''}`;
  } else {
    review.textContent = item.result_state === 'missing' ? 'Review: unavailable without a submission.' : 'Review: pending.';
  }
  article.append(review);
  return article;
}

async function loadResults() {
  const button = $('#refresh-results');
  setBusy(button, true, 'Refreshing…');
  const query = resultState.value ? `?state=${encodeURIComponent(resultState.value)}` : '';
  const { response, result } = await api(`/pursers/results${query}`);
  setBusy(button, false);
  if (!response?.ok || !result.ok || !Array.isArray(result.results)) {
    submissionList.replaceChildren();
    resultsEmpty.hidden = false;
    resultsEmpty.querySelector('h3').textContent = 'Results unavailable';
    resultsEmpty.querySelector('p').textContent = messageFor(result, 'Board results could not be read.');
    setMessage($('#results-message'), messageFor(result, 'Board results could not be read.'), 'error');
    setPill($('#results-pill'), 'Unavailable', 'warning');
    return result;
  }
  submissionList.replaceChildren(...result.results.map(makeResultRow));
  resultsEmpty.hidden = result.results.length > 0;
  if (!result.results.length) {
    resultsEmpty.querySelector('h3').textContent = 'No matching results';
    resultsEmpty.querySelector('p').textContent = 'No ticket in the bounded board response matches this state.';
  }
  const omitted = Number.isInteger(result.omitted) ? result.omitted : 0;
  const suffix = omitted > 0 ? `; ${omitted} omitted by the response bound` : '';
  setMessage($('#results-message'), `${result.results.length} result${result.results.length === 1 ? '' : 's'} shown${suffix}.`);
  setPill($('#results-pill'), `${result.results.length} shown`, result.results.length ? 'ready' : 'neutral');
  return result;
}

async function pauseSeat(slotId, button) {
  setBusy(button, true, 'Pausing…');
  const { response, result } = await api('/pursers/team/seat/pause', {
    json: {
      slot_id: slotId,
      message: 'Pause after reaching a safe checkpoint. Preserve current work and wait for operator direction.',
      reason: 'Operator requested a safe pause from Pursers Home.',
    },
  });
  setBusy(button, false);
  const text = response?.ok && result.ok
    ? 'Checkpoint request delivered. Refresh until the teammate state changes.'
    : messageFor(result, 'Pause request failed. Nothing changed.');
  showGlobal(response?.ok && result.ok ? 'Pause requested' : 'Pause needs attention', text, response?.ok && result.ok ? 'info' : 'error');
  await loadTeamStatus();
}

function openStop(member) {
  state.pendingStop = member;
  $('#stop-copy').textContent = `Request cooperative shutdown for ${member.name || 'this teammate'}? History and board work remain visible.`;
  stopDialog.showModal();
}

async function stopSeat() {
  const member = state.pendingStop;
  if (!member) return;
  const button = $('#confirm-stop');
  setBusy(button, true, 'Requesting…');
  const { response, result } = await api('/pursers/team/seat/stop', {
    json: { slot_id: member.slot_id, reason: 'Operator requested stop from Pursers Home.' },
  });
  setBusy(button, false);
  stopDialog.close();
  state.pendingStop = null;
  const text = response?.ok && result.ok
    ? 'Shutdown was requested. It is cooperative and is not complete until roster state confirms it.'
    : messageFor(result, 'Stop request failed. Nothing changed.');
  showGlobal(response?.ok && result.ok ? 'Stop requested' : 'Stop needs attention', text, response?.ok && result.ok ? 'warning' : 'error');
  await loadTeamStatus();
}

function splitTicketList(value) {
  return value.split(/[,\n]/).map((item) => item.trim()).filter(Boolean);
}

function ticketPayload() {
  const payload = {
    title: $('#ticket-title').value.trim(),
    description: $('#ticket-description').value.trim(),
    target_url: $('#ticket-target').value.trim(),
    scope: $('#ticket-scope').value,
    priority: $('#ticket-priority').value,
    tier: Number($('#ticket-tier').value),
    required_fields: splitTicketList($('#ticket-required').value),
  };
  for (const [id, key] of [['ticket-tags', 'tags'], ['ticket-files', 'related_files'], ['ticket-forbidden', 'forbidden']]) {
    const items = splitTicketList($(`#${id}`).value);
    if (items.length) payload[key] = items;
  }
  return payload;
}

function ticketTone(status) {
  if (status === 'closed') return 'ready';
  if (['canceled', 'terminated', 'rejected'].includes(status)) return 'error';
  if (['submitted', 'reviewing', 'in_review'].includes(status)) return 'warning';
  return 'neutral';
}

function makeTicketRow(ticket) {
  const row = document.createElement('article');
  row.className = 'ticket-row';
  row.tabIndex = 0;
  row.setAttribute('aria-label', `${ticket.ticket_id || 'Ticket'}: ${ticket.title || 'Untitled'}, ${ticket.status || 'unknown'}`);
  const heading = document.createElement('strong');
  heading.textContent = ticket.title || 'Untitled ticket';
  const id = document.createElement('code');
  id.textContent = ticket.ticket_id || 'Unknown ID';
  const status = document.createElement('span');
  setPill(status, ticket.status || 'unknown', ticketTone(ticket.status));
  const detail = document.createElement('p');
  detail.textContent = [ticket.priority, ticket.assigned_to ? `assigned to ${ticket.assigned_to}` : 'unassigned', ticket.created_by ? `created by ${ticket.created_by}` : null].filter(Boolean).join(' · ');
  const actions = document.createElement('div');
  actions.className = 'row-actions';
  if (!['closed', 'canceled', 'terminated'].includes(ticket.status)) {
    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'button button-danger-quiet';
    cancel.textContent = 'Cancel ticket';
    cancel.addEventListener('click', () => cancelTicket(ticket, cancel));
    actions.append(cancel);
  }
  row.append(heading, id, status, detail, actions);
  return row;
}

async function loadTickets() {
  const { response, result } = await api('/pursers/tickets');
  if (!response?.ok || !result.ok) {
    ticketList.replaceChildren();
    ticketEmpty.hidden = false;
    setPill($('#ticket-pill'), 'Needs attention', 'warning');
    setMessage(ticketMessage, messageFor(result, 'Tickets could not be loaded.'), 'error');
    return;
  }
  const tickets = Array.isArray(result.tickets) ? result.tickets : [];
  ticketList.replaceChildren(...tickets.map(makeTicketRow));
  ticketEmpty.hidden = tickets.length > 0;
  setPill($('#ticket-pill'), `${tickets.length} persisted`, tickets.length ? 'ready' : 'neutral');
}

async function createTicket(event) {
  event.preventDefault();
  if (!ticketForm.reportValidity()) return;
  const button = $('#create-ticket');
  setBusy(button, true, 'Creating…');
  const { response, result } = await api('/pursers/tickets/create', { json: ticketPayload() });
  setBusy(button, false);
  if (!response?.ok || !result.ok) {
    setMessage(ticketMessage, messageFor(result, 'Ticket creation failed. Nothing was synthesized.'), 'error');
    return;
  }
  setMessage(ticketMessage, `Created ${result.ticket.ticket_id} as unassigned board work.`, 'success');
  ticketForm.reset();
  $('#ticket-required').value = 'branch_and_commit\nfiles_changed\ntest_output';
  await loadTickets();
}

async function cancelTicket(ticket, button) {
  if (!window.confirm(`Cancel ${ticket.ticket_id}? Central will verify your authority.`)) return;
  setBusy(button, true, 'Canceling…');
  const { response, result } = await api('/pursers/tickets/cancel', {
    json: { ticket_id: ticket.ticket_id, reason: 'Canceled by an authorized operator from Pursers Home.' },
  });
  setBusy(button, false);
  if (!response?.ok || !result.ok) {
    setMessage(ticketMessage, messageFor(result, 'Central refused cancellation. Nothing changed.'), 'error');
    return;
  }
  setMessage(ticketMessage, `${ticket.ticket_id} is canceled in persisted board state.`, 'success');
  await loadTickets();
}

async function refreshAll() {
  if (!state.helper) {
    showGlobal('Connect the local helper first', 'Use the exact helper URL and access token. Neither value is persisted.', 'warning');
    return;
  }
  const button = $('#refresh-all');
  setBusy(button, true, 'Refreshing…');
  await Promise.all([loadConnection(), loadTeamStatus(), loadTickets(), loadResults()]);
  setBusy(button, false);
}

helperForm.addEventListener('submit', connectHelper);
connectionForm.addEventListener('submit', (event) => {
  event.preventDefault();
  submitDoor('connect', $('#connect-door'));
});
$('#validate-door').addEventListener('click', () => submitDoor('validate', $('#validate-door')));
$('#rotate-door').addEventListener('click', () => submitDoor('rotate', $('#rotate-door')));
$('#recover-seat').addEventListener('click', recoverConnection);
$('#add-seat').addEventListener('click', () => addSeat());
teamForm.addEventListener('submit', planTeam);
$$('#team-form input, #team-form select').forEach((input) => input.addEventListener('input', resetPlan));
confirmStart.addEventListener('change', () => {
  startTeam.disabled = !(confirmStart.checked && state.planCanApply);
});
startTeam.addEventListener('click', applyTeam);
$('#refresh-team').addEventListener('click', loadTeamStatus);
$('#refresh-roster').addEventListener('click', loadTeamStatus);
$('#refresh-tickets').addEventListener('click', loadTickets);
ticketForm.addEventListener('submit', createTicket);
$('#refresh-results').addEventListener('click', loadResults);
resultState.addEventListener('change', loadResults);
$('#refresh-all').addEventListener('click', refreshAll);
$('#confirm-stop').addEventListener('click', (event) => {
  event.preventDefault();
  stopSeat();
});
doorInput.addEventListener('paste', () => {
  window.setTimeout(() => setMessage(connectionMessage, 'Door pasted. It will be cleared as soon as you check or connect.'), 0);
});

addSeat({ role: 'worker', tier_max: 2, name: 'pursers-worker-1', folder: 'pursers-worker-1' });
addSeat({ role: 'reviewer', tier_max: 2, name: 'pursers-reviewer-1', folder: 'pursers-reviewer-1' });

updateJourney();
showGlobal('Connect the local helper first', 'AionUi loaded Pursers Home. Authenticate the loopback helper to read the selected board.', 'info');
window.__PURSERS_HOME_READY__ = true;
