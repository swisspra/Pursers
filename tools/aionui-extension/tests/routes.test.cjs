'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { createHandlers } = require('../webui/routes.js');

function segment(value) {
  return Buffer.from(JSON.stringify(value)).toString('base64url');
}

function door(board = 'demo') {
  const token = `${segment({ alg: 'RS256', kid: 'door-1' })}.${segment({ exp: 2000000000 })}.synthetic-signature`;
  return `prs1.${segment({ u: 'http://127.0.0.1:8766/mcp', b: board, r: 'worker', t: token })}`;
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

test('team status returns the real host roster and task list', async () => {
  const calls = [];
  const handlers = createHandlers({
    runTeamCli: async (command, input) => {
      calls.push({ command, input });
      if (command[0] === 'members') {
        return { success: true, data: { members: [{ slot_id: 'slot-1', name: 'worker-1', role: 'teammate' }] } };
      }
      return { success: true, data: { tasks: [{ id: 'task-1', subject: 'Ship Home' }] } };
    },
  });
  const response = await handlers.handle(new Request('http://localhost/pursers/team/status'));
  const payload = await response.json();
  assert.equal(response.status, 200);
  assert.deepEqual(payload.members, [{ slot_id: 'slot-1', name: 'worker-1', role: 'teammate' }]);
  assert.deepEqual(payload.tasks, [{ id: 'task-1', subject: 'Ship Home' }]);
  assert.deepEqual(calls.map((call) => call.command), [['members'], ['task', 'list']]);
});

test('team status uses the bounded standalone lifecycle projection when configured', async () => {
  let nativeCalls = 0;
  const handlers = createHandlers({
    runTeamCli: async () => { nativeCalls += 1; return { success: false }; },
    runStandaloneTeamStatus: async () => ({
      ok: true,
      op: 'status',
      mode: 'board_managed_standalone',
      native_team: false,
      board: 'sandbox-home',
      group_revision: 2,
      group_count: 1,
      members: [{ agent_id: 'AI-worker-1', name: 'worker-1', role: 'worker', status: 'active', lifecycle_status: 'active' }],
      tasks: [],
    }),
  });
  const response = await handlers.handle(new Request('http://localhost/pursers/team/status'));
  const payload = await response.json();
  assert.equal(response.status, 200);
  assert.equal(payload.mode, 'board_managed_standalone');
  assert.equal(payload.native_team, false);
  assert.equal(payload.members[0].name, 'worker-1');
  assert.equal('slot_id' in payload.members[0], false);
  assert.equal(nativeCalls, 0);
});

test('team plan is dry-run and live apply requires the adapter confirmation contract', async () => {
  const calls = [];
  const runTeamCli = async (command, input) => {
    calls.push({ command, input });
    if (command[0] === 'members') {
      return {
        success: true,
        data: { members: [{ slot_id: 'lead-1', name: 'lead', role: 'lead', assistant_id: 'lead-assistant' }] },
      };
    }
    if (command[0] === 'list-assistants') {
      return { success: true, data: { assistants: [{ assistant_id: 'seat-assistant' }] } };
    }
    if (command[0] === 'spawn-agent') {
      return { success: true, data: { slot_id: 'seat-1' } };
    }
    return { success: true, data: {} };
  };
  const handlers = createHandlers({ runTeamCli });
  const spec = {
    team: { name: 'Demo' },
    lead: { name: 'lead', assistant_id: 'lead-assistant' },
    seats: [{ name: 'worker-1', assistant_id: 'seat-assistant', role: 'worker', tier_max: 2, folder: 'worker-1' }],
    options: { dry_run: true },
  };
  const plan = await handlers.handle(new Request('http://localhost/pursers/team/plan', {
    method: 'POST', body: JSON.stringify(spec),
  }));
  assert.equal(plan.status, 200);
  assert.equal((await plan.json()).dry_run, true);
  assert.equal(calls.some((call) => call.command[0] === 'spawn-agent'), false);

  spec.options = { confirm: 'apply-live', dry_run: false, send_kickoff: true };
  const apply = await handlers.handle(new Request('http://localhost/pursers/team/apply', {
    method: 'POST', body: JSON.stringify(spec),
  }));
  const payload = await apply.json();
  assert.equal(apply.status, 200);
  assert.equal(payload.dry_run, false);
  assert.deepEqual(
    calls.filter((call) => ['spawn-agent', 'send-message'].includes(call.command[0])).map((call) => call.command),
    [['spawn-agent'], ['send-message']],
  );
});

test('seat controls map only to bounded interrupt and cooperative shutdown calls', async () => {
  const calls = [];
  const handlers = createHandlers({
    runTeamCli: async (command, input) => {
      calls.push({ command, input });
      return { success: true, data: {} };
    },
  });
  const pause = await handlers.handle(new Request('http://localhost/pursers/team/seat/pause', {
    method: 'POST', body: JSON.stringify({ slot_id: 'slot-1', message: 'Checkpoint first.', reason: 'operator' }),
  }));
  const stop = await handlers.handle(new Request('http://localhost/pursers/team/seat/stop', {
    method: 'POST', body: JSON.stringify({ slot_id: 'slot-1', reason: 'operator' }),
  }));
  assert.equal(pause.status, 200);
  assert.equal(stop.status, 200);
  assert.deepEqual(calls, [
    { command: ['interrupt-agent'], input: { slot_id: 'slot-1', message: 'Checkpoint first.', reason: 'operator' } },
    { command: ['shutdown-agent'], input: { slot_id: 'slot-1', reason: 'operator' } },
  ]);
});

test('helper mode defers MCP import and binds onboarding to one board', async () => {
  const secretDoor = door();
  const handlers = createHandlers({
    expectedBoard: 'sandbox-home',
    allowedOrigin: 'http://127.0.0.1:25808',
    runBridge: async () => 'push_mode=push\nboard=sandbox-home role=worker kid=door-1 exp=2000000000 seat_names_used=worker-1\n',
    importMcp: async () => ({ success: true, imported: false }),
  });
  const wrongBoard = await handlers.handle(new Request('http://127.0.0.1:43121/pursers/onboarding/validate', {
    method: 'POST',
    headers: { origin: 'http://127.0.0.1:25808' },
    body: JSON.stringify({ door: secretDoor, expected_role: 'worker' }),
  }));
  assert.equal(wrongBoard.status, 422);
  assert.equal((await wrongBoard.json()).code, 'wrong_board');

  const wrongOrigin = await handlers.handle(new Request('http://127.0.0.1:43121/pursers/onboarding/status', {
    headers: { origin: 'http://localhost:25808' },
  }));
  assert.equal(wrongOrigin.status, 403);

  const connected = await handlers.handle(new Request('http://127.0.0.1:43121/pursers/onboarding/connect', {
    method: 'POST',
    headers: { origin: 'http://127.0.0.1:25808' },
    body: JSON.stringify({ door: door('sandbox-home'), expected_role: 'worker', seat_name: 'worker-1' }),
  }));
  const payload = await connected.json();
  assert.equal(connected.status, 200);
  assert.equal(payload.imported, false);
  assert.deepEqual(payload.mcp_definition, {
    name: 'Pursers worker sandbox-home',
    transport: { type: 'stdio', command: 'pursers-wait-bridge', args: [], env: {} },
  });
});

test('standalone seat routes preserve exact identity and map lifecycle outcomes', async () => {
  let current = {
    board: 'sandbox-home', agent_id: 'AI-worker-1', principal_id: 'PR-worker',
    agent_name: 'worker-1', role: 'worker', lifecycle_status: 'active',
  };
  const calls = [];
  const handlers = createHandlers({
    expectedBoard: 'sandbox-home',
    seatLifecycleDependencies: {
      joinSeat: async (input) => { calls.push(['join', input]); return { ok: true, identity: current, rejoined: false }; },
      readBoard: async (input) => { calls.push(['read', input]); return { ok: true, board_id: 'sandbox-home', agents: [{ ...current, status: 'idle', lease_expires_at: null }] }; },
      retireSelf: async (input) => { calls.push(['retire', input]); current = { ...current, lifecycle_status: 'retired' }; return { ok: true, board_id: 'sandbox-home', agent: current }; },
      forgetDoor: async (input) => { calls.push(['forget', input]); return { ok: true, ...input }; },
    },
  });
  const joined = await handlers.handle(new Request('http://localhost/pursers/seat-lifecycle/join', {
    method: 'POST', body: JSON.stringify({ board: 'sandbox-home', door: 'not-returned', agent_name: 'worker-1', role: 'worker', tier_max: 2 }),
  }));
  assert.equal(joined.status, 200);
  assert.equal(JSON.stringify(await joined.json()).includes('not-returned'), false);
  const disconnected = await handlers.handle(new Request('http://localhost/pursers/seat-lifecycle/disconnect', {
    method: 'POST', body: JSON.stringify({ board: 'sandbox-home', confirm: 'retire worker-1 from sandbox-home' }),
  }));
  assert.equal(disconnected.status, 200);
  assert.equal((await disconnected.json()).outcome, 'disconnected');
  assert.deepEqual(calls.map(([name]) => name), ['join', 'read', 'read', 'retire', 'read', 'forget']);
});
