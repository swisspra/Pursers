'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { createHandlers } = require('../webui/routes.js');

test('join stores through the fake bridge and imports an env-free MCP server', async () => {
  const calls = [];
  const runBridge = async (args) => {
    calls.push([...args]);
    if (args[0] === 'join') {
      return 'board=demo\nrole=worker\nseat_name=worker-host-1\npush=yes\nverifier=accepted\n';
    }
    return 'push_mode=push\nboard=demo role=worker kid=door-1 exp=2000000000 seat_names_used=worker-host-1\n';
  };
  let imported;
  const fetchImpl = async (url, options) => {
    imported = { url: String(url), options, body: JSON.parse(options.body) };
    return { ok: true, json: async () => ({ success: true }) };
  };
  const handlers = createHandlers({ runBridge, fetchImpl });
  const secretDoor = 'door-value-used-only-by-fake-bridge';
  const request = new Request('http://127.0.0.1:8765/pursers/join', {
    method: 'POST',
    headers: { cookie: 'session=redacted', 'x-csrf-token': 'redacted' },
    body: JSON.stringify({ door: secretDoor }),
  });
  const response = await handlers.handle(request);
  const payload = await response.json();

  assert.equal(response.status, 200);
  assert.deepEqual(calls, [['join', secretDoor], ['status']]);
  assert.equal(imported.url, 'http://127.0.0.1:8765/api/mcp/servers/import');
  assert.deepEqual(imported.body.servers[0], {
    name: 'Pursers worker demo',
    transport: { type: 'stdio', command: 'pursers-wait-bridge', args: [], env: {} },
    builtin: false,
    enabled: true,
  });
  assert.deepEqual(payload.status, {
    board: 'demo',
    role: 'worker',
    seat_name: 'worker-host-1',
    push_mode: 'push',
    kid: 'door-1',
    exp: 2000000000,
  });
  assert.equal(JSON.stringify(payload).includes(secretDoor), false);
});

test('status returns only redacted bridge fields', async () => {
  const handlers = createHandlers({
    runBridge: async (args) => {
      assert.deepEqual(args, ['status']);
      return 'push_mode=push\nboard=demo role=reviewer kid=door-2 exp=2000000001 seat_names_used=reviewer-host-1\n';
    },
  });
  const response = await handlers.handle(new Request('http://localhost/pursers/status'));
  const payload = await response.json();
  assert.equal(response.status, 200);
  assert.deepEqual(payload, {
    ok: true,
    push_mode: 'push',
    seats: [{
      board: 'demo',
      role: 'reviewer',
      kid: 'door-2',
      exp: 2000000001,
      seat_names: ['reviewer-host-1'],
    }],
  });
});

test('missing bridge produces a bounded install hint without echoing the door', async () => {
  const missing = Object.assign(new Error('not found'), { code: 'ENOENT' });
  const handlers = createHandlers({ runBridge: async () => { throw missing; } });
  const secretDoor = 'another-door-value';
  const response = await handlers.handle(new Request('http://localhost/pursers/join', {
    method: 'POST',
    body: JSON.stringify({ door: secretDoor }),
  }));
  const text = await response.text();
  assert.equal(response.status, 503);
  assert.match(text, /uv tool install/);
  assert.equal(text.includes(secretDoor), false);
});
