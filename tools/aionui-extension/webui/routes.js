'use strict';

const { execFile } = require('node:child_process');
const { createDoorOnboarding } = require('../door/adapter.cjs');
const { isLoopbackHostname } = require('../security/loopback.cjs');
const { createTeamAdapter } = require('../team/adapter.cjs');
const { createTicketLifecycleAdapter } = require('../ticket_lifecycle/adapter.cjs');
const { createResultVisibility } = require('../result_visibility/adapter.cjs');
const { createTeamLifecycleAdapter } = require('../team_lifecycle/adapter.cjs');
const { createSeatLifecycle } = require('../seat_lifecycle/adapter.cjs');

const BRIDGE_COMMAND = 'pursers-wait-bridge';
const INSTALL_HINT =
  'Install the bridge with `uv tool install pursers-wait-bridge` or `pipx install pursers-wait-bridge`, then try again.';

function runBridge(args, options = {}) {
  return new Promise((resolve, reject) => {
    execFile(
      BRIDGE_COMMAND,
      args,
      {
        encoding: 'utf8',
        maxBuffer: 1024 * 1024,
        timeout: 30000,
        env: { ...process.env, ...(options.env || {}) },
      },
      (error, stdout, stderr) => {
        if (error) {
          error.stderr = String(stderr || '').slice(0, 4096);
          reject(error);
          return;
        }
        resolve(stdout);
      },
    );
  });
}

function jsonResponse(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json; charset=utf-8' },
  });
}

function forwardedHeaders(request) {
  const headers = { 'content-type': 'application/json' };
  for (const name of ['cookie', 'x-csrf-token']) {
    const value = request.headers.get(name);
    if (value) {
      headers[name] = value;
    }
  }
  return headers;
}

function loopbackRequest(request, allowedOrigin) {
  const url = new URL(request.url);
  const origin = request.headers.get('origin');
  if (!isLoopbackHostname(url.hostname)) return false;
  if (allowedOrigin) return origin === allowedOrigin;
  return !origin || origin === url.origin;
}

function responseStatus(value) {
  if (value.ok) return 200;
  if (value.code === 'bridge_not_installed') return 503;
  if (value.code === 'server_unreachable' || value.code === 'bridge_status_failed' || value.code === 'mcp_registration_failed' || value.code === 'invalid_bridge_response') return 502;
  if (value.code === 'identity_conflict' || value.code === 'rotation_required' || value.code === 'rotation_requires_existing') return 409;
  return 422;
}

function teamResponseStatus(value) {
  if (value.ok) return 200;
  const code = value.error && value.error.code;
  if (code === 'permission_denied' || code === 'not_in_team') return 403;
  if (code === 'runtime_context_missing' || code === 'runtime_auth_failed') return 409;
  if (code === 'transport_unavailable') return 503;
  return 422;
}

function ticketResponseStatus(value) {
  if (value.ok) return 200;
  const code = value.error && value.error.code;
  if (code === 'permission_denied') return 403;
  if (code === 'ticket_not_found') return 404;
  if (code === 'conflict') return 409;
  if (code === 'backend_unavailable' || code === 'not_connected') return 503;
  if (code === 'board_mismatch' || code === 'invalid_input') return 422;
  return 502;
}

function resultsResponseStatus(value) {
  if (value.ok) return 200;
  if (value.code === 'invalid_ticket_id' || value.code === 'invalid_result_state') return 400;
  if (value.code === 'ticket_not_found') return 404;
  if (value.code === 'backend_unavailable') return 503;
  return 502;
}

function groupResponseStatus(value) {
  if (value.ok) return 200;
  const code = value.error && value.error.code;
  if (code === 'permission_denied') return 403;
  if (code === 'group_not_found') return 404;
  if (code === 'conflict') return 409;
  if (code === 'backend_unavailable' || code === 'not_connected') return 503;
  return 422;
}

function seatResponseStatus(value) {
  if (value.ok) return 200;
  if (['invalid_input', 'invalid_agent_name', 'invalid_role', 'reserved_identity_selector', 'wrong_board'].includes(value.code)) return 400;
  if (['identity_conflict', 'identity_mismatch', 'active_lease'].includes(value.code)) return 409;
  if (['board_unavailable', 'join_failed', 'retirement_failed', 'not_connected'].includes(value.code)) return 503;
  if (['invalid_board_response', 'invalid_join_response', 'invalid_retirement_response'].includes(value.code)) return 502;
  return 422;
}

async function readBody(request) {
  try {
    const body = await request.json();
    return body && typeof body === 'object' && !Array.isArray(body) ? body : null;
  } catch (_error) {
    return null;
  }
}

function createHandlers(dependencies = {}) {
  const invokeBridge = dependencies.runBridge || runBridge;
  const fetchImpl = dependencies.fetchImpl || globalThis.fetch;
  const expectedBoard = dependencies.expectedBoard || null;
  const expectedCentral = dependencies.expectedCentral || null;
  const importMcp = dependencies.importMcp || (async (server, context) => {
    const request = context.request;
    const endpoint = new URL('/api/mcp/servers/import', new URL(request.url).origin);
    const response = await fetchImpl(endpoint, {
      method: 'POST',
      headers: forwardedHeaders(request),
      body: JSON.stringify({ servers: [{ ...server, builtin: false, enabled: true }] }),
    });
    if (!response.ok) return { success: false };
    return response.json();
  });
  const onboarding = createDoorOnboarding({
    runBridge: invokeBridge,
    nowEpoch: dependencies.nowEpoch,
    importMcp,
  });
  const team = createTeamAdapter({ runCli: dependencies.runTeamCli });
  const tickets = dependencies.runTicketLifecycle && expectedBoard
    ? createTicketLifecycleAdapter({ run: dependencies.runTicketLifecycle, expectedBoard })
    : null;
  const results = expectedBoard && expectedCentral
    ? createResultVisibility({
      expectedBoard,
      expectedCentral,
      fetchBoard: dependencies.fetchResults || (async () => {
        throw new Error('result backend unavailable');
      }),
    })
    : { read: async () => ({ ok: false, code: 'backend_unavailable', retryable: true }) };
  const groups = dependencies.runTeamLifecycle
    ? createTeamLifecycleAdapter({ expectedBoard, run: dependencies.runTeamLifecycle })
    : null;
  const seatLifecycle = expectedBoard && dependencies.seatLifecycleDependencies
    ? createSeatLifecycle({ expectedBoard, ...dependencies.seatLifecycleDependencies })
    : null;

  function scopedStatus(current) {
    if (!current.ok || !expectedBoard) return current;
    return { ...current, seats: current.seats.filter((seat) => seat.board === expectedBoard) };
  }

  function scopedInput(body) {
    return expectedBoard ? { ...body, expected_board: expectedBoard } : body;
  }

  async function status() {
    const current = scopedStatus(await onboarding.status());
    if (current.ok) return jsonResponse(200, { ok: true, push_mode: current.push_mode, seats: current.seats });
    const body = { ok: false, error: current.code };
    if (current.code === 'bridge_not_installed') body.install_hint = INSTALL_HINT;
    return jsonResponse(responseStatus(current), body);
  }

  async function join(request) {
    const body = await readBody(request);
    if (!body) return jsonResponse(400, { ok: false, error: 'invalid_json' });
    const connected = await onboarding.connect(scopedInput(body), { request });
    if (!connected.ok) {
      const payload = { ok: false, error: connected.code };
      if (connected.code === 'bridge_not_installed') payload.install_hint = INSTALL_HINT;
      if (connected.connected) payload.joined = true;
      const invalidInput = new Set(['invalid_door', 'invalid_board', 'invalid_role', 'invalid_url', 'insecure_remote_url', 'expired_door', 'invalid_seat_name', 'invalid_tier', 'invalid_folder']);
      return jsonResponse(invalidInput.has(connected.code) ? 400 : responseStatus(connected), payload);
    }
    return jsonResponse(200, {
      ok: true,
      status: connected.status,
      mcp_server: connected.mcp_server,
      mcp_definition: connected.mcp_definition,
      imported: connected.imported,
    });
  }

  async function typed(request, operation) {
    const body = request.method === 'GET' ? {} : await readBody(request);
    if (body === null) return jsonResponse(400, { ok: false, operation, outcome: 'failed', code: 'invalid_json', message: 'Request body must be a JSON object.', retryable: false });
    if (expectedBoard && operation === 'recover' && body.board !== expectedBoard) {
      return jsonResponse(422, { ok: false, operation, outcome: 'failed', code: 'wrong_board', message: `Use the configured board ${expectedBoard}.`, retryable: false });
    }
    const value = operation === 'status'
      ? scopedStatus(await onboarding.status())
      : operation === 'validate'
        ? onboarding.validate(scopedInput(body))
        : await onboarding[operation](scopedInput(body), { request });
    return jsonResponse(responseStatus(value), value);
  }

  async function teamTyped(request, operation) {
    const body = request.method === 'GET' ? {} : await readBody(request);
    if (body === null) {
      return jsonResponse(400, {
        ok: false,
        op: operation,
        error: { code: 'invalid_json', message: 'Request body must be a JSON object.' },
      });
    }
    let value;
    if (operation === 'status') value = await team.status({ tasks: true });
    else if (operation === 'plan') value = await team.plan(body);
    else if (operation === 'apply') value = await team.apply(body);
    else if (operation === 'pause') value = await team.pauseSeat(body.slot_id, body.message, body.reason);
    else value = await team.stopSeat(body.slot_id, body.reason);
    return jsonResponse(teamResponseStatus(value), value);
  }

  async function ticketTyped(request, operation) {
    if (!tickets) return jsonResponse(503, {
      ok: false,
      error: { code: 'not_connected', message: 'Connect the authenticated loopback helper first.', retryable: true },
    });
    const body = request.method === 'GET' ? {} : await readBody(request);
    if (body === null) return jsonResponse(400, {
      ok: false,
      error: { code: 'invalid_input', message: 'Request body must be a JSON object.', retryable: false },
    });
    const value = await tickets[operation](body);
    return jsonResponse(ticketResponseStatus(value), value);
  }

  async function groupTyped(request, operation) {
    if (!groups) return jsonResponse(503, { ok: false, error: { code: 'not_connected', message: 'Connect a Pursers board first.', retryable: true } });
    const body = request.method === 'GET' ? {} : await readBody(request);
    if (body === null) return jsonResponse(400, { ok: false, error: { code: 'invalid_input', message: 'Request body must be a JSON object.', retryable: false } });
    const value = await groups[operation](body);
    return jsonResponse(groupResponseStatus(value), value);
  }

  async function seatTyped(request, operation, url) {
    if (!seatLifecycle) return jsonResponse(503, {
      ok: false, operation, outcome: 'failed', code: 'not_connected',
      message: 'Connect the authenticated loopback helper first.', retryable: true,
    });
    const body = request.method === 'GET'
      ? Object.fromEntries(['agent_name', 'role'].flatMap((name) => {
        const value = url.searchParams.get(name);
        return value === null ? [] : [[name, value]];
      }))
      : await readBody(request);
    if (body === null) return jsonResponse(400, {
      ok: false, operation, outcome: 'failed', code: 'invalid_input',
      message: 'Request body must be a JSON object.', retryable: false,
    });
    const value = await seatLifecycle[operation](body);
    return jsonResponse(seatResponseStatus(value), value);
  }

  async function handle(request) {
    const url = new URL(request.url);
    if (!loopbackRequest(request, dependencies.allowedOrigin)) return jsonResponse(403, { ok: false, error: 'loopback_same_origin_required' });
    if (request.method === 'POST' && url.pathname === '/pursers/join') {
      return join(request);
    }
    if (request.method === 'GET' && url.pathname === '/pursers/status') {
      return status();
    }
    if (request.method === 'POST' && url.pathname === '/pursers/onboarding/validate') return typed(request, 'validate');
    if (request.method === 'POST' && url.pathname === '/pursers/onboarding/connect') return typed(request, 'connect');
    if (request.method === 'GET' && url.pathname === '/pursers/onboarding/status') return typed(request, 'status');
    if (request.method === 'POST' && url.pathname === '/pursers/onboarding/rotate') return typed(request, 'rotate');
    if (request.method === 'POST' && url.pathname === '/pursers/onboarding/recover') return typed(request, 'recover');
    if (request.method === 'GET' && url.pathname === '/pursers/team/status') return teamTyped(request, 'status');
    if (request.method === 'POST' && url.pathname === '/pursers/team/plan') return teamTyped(request, 'plan');
    if (request.method === 'POST' && url.pathname === '/pursers/team/apply') return teamTyped(request, 'apply');
    if (request.method === 'POST' && url.pathname === '/pursers/team/seat/pause') return teamTyped(request, 'pause');
    if (request.method === 'POST' && url.pathname === '/pursers/team/seat/stop') return teamTyped(request, 'stop');
    if (request.method === 'GET' && url.pathname === '/pursers/tickets/status') return ticketTyped(request, 'status');
    if (request.method === 'GET' && url.pathname === '/pursers/tickets') return ticketTyped(request, 'list');
    if (request.method === 'POST' && url.pathname === '/pursers/tickets/get') return ticketTyped(request, 'get');
    if (request.method === 'POST' && url.pathname === '/pursers/tickets/create') return ticketTyped(request, 'create');
    if (request.method === 'POST' && url.pathname === '/pursers/tickets/cancel') return ticketTyped(request, 'cancel');
    if (request.method === 'POST' && url.pathname === '/pursers/seat-lifecycle/join') return seatTyped(request, 'join', url);
    if (request.method === 'GET' && url.pathname === '/pursers/seat-lifecycle/status') return seatTyped(request, 'status', url);
    if (request.method === 'POST' && url.pathname === '/pursers/seat-lifecycle/disconnect') return seatTyped(request, 'disconnect', url);
    if (request.method === 'GET' && url.pathname === '/pursers/results') {
      const value = await results.read({
        ticketId: url.searchParams.get('ticket_id'),
        state: url.searchParams.get('state'),
      });
      return jsonResponse(resultsResponseStatus(value), value);
    }
    if (request.method === 'GET' && url.pathname === '/pursers/groups/status') return groupTyped(request, 'status');
    if (request.method === 'GET' && url.pathname === '/pursers/groups') return groupTyped(request, 'list');
    if (request.method === 'POST' && url.pathname === '/pursers/groups/create') return groupTyped(request, 'create');
    if (request.method === 'POST' && url.pathname === '/pursers/groups/update') return groupTyped(request, 'update');
    if (request.method === 'POST' && url.pathname === '/pursers/groups/remove') return groupTyped(request, 'remove');
    return jsonResponse(404, { ok: false, error: 'not_found' });
  }

  return { groups, handle, join, onboarding, results, seatLifecycle, status, team, tickets };
}

const defaultHandlers = createHandlers();
module.exports = {
  BRIDGE_COMMAND,
  INSTALL_HINT,
  createHandlers,
  handle: defaultHandlers.handle,
  fetch: defaultHandlers.handle,
  join: defaultHandlers.join,
  status: defaultHandlers.status,
  default: defaultHandlers.handle,
};
