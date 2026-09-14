'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { createHandlers } = require('../webui/routes.js');

function segment(value) {
  return Buffer.from(JSON.stringify(value)).toString('base64url');
}

function door() {
  const token = `${segment({ alg: 'RS256', kid: 'door-1' })}.${segment({ exp: 2000000000 })}.synthetic-signature`;
  return `prs1.${segment({ u: 'http://127.0.0.1:8766/mcp', b: 'demo', r: 'worker', t: token })}`;
}

test('join stores through the fake bridge and imports an env-free MCP server', async () => {
  const calls = [];
  const runBridge = async (args) => {
    calls.push([...args]);
    if (args[0] === 'join') {
      return 'board=demo\nrole=worker\nseat_name=worker-host-1\npush=yes\nverifier=accepted\n';
    }
    if (calls.length === 1) return 'push_mode=push\n';
    return 'push_mode=push\nboard=demo role=worker kid=door-1 exp=2000000000 seat_names_used=worker-host-1\n';
  };
  let imported;
  const fetchImpl = async (url, options) => {
    imported = { url: String(url), options, body: JSON.parse(options.body) };
    return { ok: true, json: async () => ({ success: true }) };
  };
  const handlers = createHandlers({ runBridge, fetchImpl });
  const secretDoor = door();
  const request = new Request('http://127.0.0.1:8765/pursers/join', {
    method: 'POST',
    headers: { cookie: 'session=redacted', 'x-csrf-token': 'redacted' },
    body: JSON.stringify({ door: secretDoor }),
  });
  const response = await handlers.handle(request);
  const payload = await response.json();

  assert.equal(response.status, 200);
  assert.deepEqual(calls, [['status'], ['join', secretDoor], ['status']]);
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

test('partial legacy join exposes redacted recovery target and recover uses no door', async () => {
  const calls = [];
  let importCalls = 0;
  const runBridge = async (args) => {
    calls.push([...args]);
    if (args[0] === 'join') {
      return 'board=demo\nrole=worker\nseat_name=worker-host-1\npush=yes\n';
    }
    return calls.length === 1
      ? 'push_mode=push\n'
      : 'push_mode=push\nboard=demo role=worker kid=door-1 exp=2000000000 seat_names_used=worker-host-1\n';
  };
  const handlers = createHandlers({
    runBridge,
    fetchImpl: async () => ({
      ok: ++importCalls > 1,
      json: async () => ({ success: true }),
    }),
  });
  const secretDoor = door();
  const joined = await handlers.handle(new Request('http://127.0.0.1:8765/pursers/join', {
    method: 'POST',
    body: JSON.stringify({ door: secretDoor, seat_name: 'worker-host-1' }),
  }));
  const partial = await joined.json();

  assert.equal(joined.status, 502);
  assert.equal(partial.outcome, 'partial');
  assert.equal(partial.joined, true);
  assert.deepEqual(partial.status, {
    board: 'demo',
    role: 'worker',
    seat_name: 'worker-host-1',
    push_mode: 'push',
    kid: 'door-1',
    exp: 2000000000,
  });
  assert.equal(JSON.stringify(partial).includes(secretDoor), false);

  const recoveryBody = {
    board: partial.status.board,
    role: partial.status.role,
    seat_name: partial.status.seat_name,
    tier_max: 2,
  };
  const recovered = await handlers.handle(new Request('http://127.0.0.1:8765/pursers/onboarding/recover', {
    method: 'POST',
    body: JSON.stringify(recoveryBody),
  }));

  assert.equal(recovered.status, 200);
  assert.equal((await recovered.json()).outcome, 'recovered');
  assert.equal(Object.hasOwn(recoveryBody, 'door'), false);
  assert.equal(importCalls, 2);
  assert.equal(calls.filter((args) => args[0] === 'join').length, 1);
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
  const secretDoor = door();
  const response = await handlers.handle(new Request('http://localhost/pursers/join', {
    method: 'POST',
    body: JSON.stringify({ door: secretDoor }),
  }));
  const text = await response.text();
  assert.equal(response.status, 503);
  assert.match(text, /uv tool install/);
  assert.equal(text.includes(secretDoor), false);
});

test('legacy join retains invalid-door 400 behavior', async () => {
  const handlers = createHandlers({ runBridge: async () => 'push_mode=push\n' });
  const response = await handlers.handle(new Request('http://127.0.0.1:8765/pursers/join', {
    method: 'POST',
    body: JSON.stringify({ door: 'not-a-door' }),
  }));
  assert.equal(response.status, 400);
  assert.deepEqual(await response.json(), { ok: false, error: 'invalid_door' });
});

test('typed validation returns redacted metadata and preserved tier', async () => {
  const handlers = createHandlers({ runBridge: async () => 'push_mode=push\n' });
  const secretDoor = door();
  const response = await handlers.handle(new Request('http://127.0.0.1:8765/pursers/onboarding/validate', {
    method: 'POST',
    body: JSON.stringify({ door: secretDoor, seat_name: 'worker-one', tier_max: 2 }),
  }));
  const payload = await response.json();
  assert.equal(response.status, 200);
  assert.equal(payload.operation, 'validate');
  assert.equal(payload.metadata.central_host, '127.0.0.1:8766');
  assert.equal(payload.metadata.board, 'demo');
  assert.equal(payload.metadata.kid, 'door-1');
  assert.equal(payload.normalized.tier_max, 2);
  assert.equal(JSON.stringify(payload).includes(secretDoor), false);
});

test('typed mutation routes refuse non-loopback or cross-origin requests', async () => {
  const handlers = createHandlers({ runBridge: async () => 'push_mode=push\n' });
  const remote = await handlers.handle(new Request('https://host.example/pursers/onboarding/connect', {
    method: 'POST',
    body: '{}',
  }));
  const crossOrigin = await handlers.handle(new Request('http://127.0.0.1:8765/pursers/onboarding/connect', {
    method: 'POST',
    headers: { origin: 'http://localhost:9999' },
    body: '{}',
  }));
  const deceptiveDns = await handlers.handle(new Request('http://127.attacker.example/pursers/status'));
  const validIpv4 = await handlers.handle(new Request('http://127.42.7.9/pursers/status'));
  const localhostSubdomain = await handlers.handle(new Request('http://worker.localhost/pursers/status'));
  const ipv6 = await handlers.handle(new Request('http://[::1]/pursers/status'));
  assert.equal(remote.status, 403);
  assert.equal(crossOrigin.status, 403);
  assert.equal(deceptiveDns.status, 403);
  assert.equal(validIpv4.status, 200);
  assert.equal(localhostSubdomain.status, 200);
  assert.equal(ipv6.status, 200);
});
