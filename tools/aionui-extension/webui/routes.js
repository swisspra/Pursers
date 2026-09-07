'use strict';

const { execFile } = require('node:child_process');

const BRIDGE_COMMAND = 'pursers-wait-bridge';
const INSTALL_HINT =
  'Install the bridge with `uv tool install pursers-wait-bridge` or `pipx install pursers-wait-bridge`, then try again.';

function runBridge(args) {
  return new Promise((resolve, reject) => {
    execFile(
      BRIDGE_COMMAND,
      args,
      { encoding: 'utf8', maxBuffer: 1024 * 1024, timeout: 30000 },
      (error, stdout) => {
        if (error) {
          reject(error);
          return;
        }
        resolve(stdout);
      },
    );
  });
}

function keyValues(line) {
  const result = {};
  for (const field of line.trim().split(/\s+/)) {
    const separator = field.indexOf('=');
    if (separator > 0) {
      result[field.slice(0, separator)] = field.slice(separator + 1);
    }
  }
  return result;
}

function parseJoin(output) {
  return Object.assign({}, ...output.split(/\r?\n/).filter(Boolean).map(keyValues));
}

function parseStatus(output) {
  const lines = output.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  let pushMode = 'unknown';
  const seats = [];
  for (const line of lines) {
    const values = keyValues(line);
    if (values.push_mode) {
      pushMode = values.push_mode;
    }
    if (values.board && values.role) {
      seats.push({
        board: values.board,
        role: values.role,
        kid: values.kid || 'unknown',
        exp: Number(values.exp) || 0,
        seat_names: values.seat_names_used === '-' ? [] : (values.seat_names_used || '').split(',').filter(Boolean),
      });
    }
  }
  return { push_mode: pushMode, seats };
}

function jsonResponse(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json; charset=utf-8' },
  });
}

function bridgeMissing(error) {
  return error && (error.code === 'ENOENT' || error.errno === -2);
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

function createHandlers(dependencies = {}) {
  const invokeBridge = dependencies.runBridge || runBridge;
  const fetchImpl = dependencies.fetchImpl || globalThis.fetch;

  async function status() {
    try {
      const parsed = parseStatus(await invokeBridge(['status']));
      return jsonResponse(200, { ok: true, ...parsed });
    } catch (error) {
      if (bridgeMissing(error)) {
        return jsonResponse(503, { ok: false, error: 'bridge_not_installed', install_hint: INSTALL_HINT });
      }
      return jsonResponse(502, { ok: false, error: 'bridge_status_failed' });
    }
  }

  async function join(request) {
    let body;
    try {
      body = await request.json();
    } catch (_error) {
      return jsonResponse(400, { ok: false, error: 'invalid_json' });
    }
    const door = typeof body.door === 'string' ? body.door.trim() : '';
    if (!door || door.length > 16384) {
      return jsonResponse(400, { ok: false, error: 'invalid_door' });
    }

    let joined;
    let current;
    try {
      joined = parseJoin(await invokeBridge(['join', door]));
      current = parseStatus(await invokeBridge(['status']));
    } catch (error) {
      if (bridgeMissing(error)) {
        return jsonResponse(503, { ok: false, error: 'bridge_not_installed', install_hint: INSTALL_HINT });
      }
      return jsonResponse(422, { ok: false, error: 'join_failed' });
    }

    if (!joined.board || !joined.role || !joined.seat_name) {
      return jsonResponse(502, { ok: false, error: 'invalid_bridge_response' });
    }

    const serverName = `Pursers ${joined.role} ${joined.board}`;
    const importBody = {
      servers: [
        {
          name: serverName,
          transport: { type: 'stdio', command: BRIDGE_COMMAND, args: [], env: {} },
          builtin: false,
          enabled: true,
        },
      ],
    };
    let imported;
    try {
      const endpoint = new URL('/api/mcp/servers/import', new URL(request.url).origin);
      const response = await fetchImpl(endpoint, {
        method: 'POST',
        headers: forwardedHeaders(request),
        body: JSON.stringify(importBody),
      });
      if (!response.ok) {
        return jsonResponse(502, { ok: false, error: 'mcp_import_failed', joined: true });
      }
      imported = await response.json();
    } catch (_error) {
      return jsonResponse(502, { ok: false, error: 'mcp_import_failed', joined: true });
    }

    const stored = current.seats.find(
      (seat) => seat.board === joined.board && seat.role === joined.role,
    );
    return jsonResponse(200, {
      ok: true,
      status: {
        board: joined.board,
        role: joined.role,
        seat_name: joined.seat_name,
        push_mode: current.push_mode,
        kid: stored ? stored.kid : 'unknown',
        exp: stored ? stored.exp : 0,
      },
      mcp_server: serverName,
      imported: imported.success !== false,
    });
  }

  async function handle(request) {
    const url = new URL(request.url);
    if (request.method === 'POST' && url.pathname === '/pursers/join') {
      return join(request);
    }
    if (request.method === 'GET' && url.pathname === '/pursers/status') {
      return status();
    }
    return jsonResponse(404, { ok: false, error: 'not_found' });
  }

  return { handle, join, status };
}

const defaultHandlers = createHandlers();
module.exports = {
  BRIDGE_COMMAND,
  INSTALL_HINT,
  createHandlers,
  handle: defaultHandlers.handle,
  join: defaultHandlers.join,
  status: defaultHandlers.status,
  default: defaultHandlers.handle,
};
