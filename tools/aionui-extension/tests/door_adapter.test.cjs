'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { createDoorOnboarding } = require('../door/adapter.cjs');

function segment(value) {
  return Buffer.from(JSON.stringify(value)).toString('base64url');
}

function door({ url = 'http://127.0.0.1:8766/mcp', board = 'sandbox', role = 'worker', kid = 'door-test-v1', exp = 2000000000 } = {}) {
  const token = `${segment({ alg: 'RS256', kid })}.${segment({ exp })}.synthetic-signature`;
  return `prs1.${segment({ u: url, b: board, r: role, t: token })}`;
}

function adapter(dependencies = {}) {
  return createDoorOnboarding({
    nowEpoch: () => 1900000000,
    importMcp: async () => ({ success: true }),
    ...dependencies,
  });
}

test('parse and validate expose metadata without door or Central URL', () => {
  const secretDoor = door();
  const onboarding = adapter({ runBridge: async () => '' });
  const parsed = onboarding.parse(secretDoor);
  const validated = onboarding.validate({
    door: secretDoor,
    expected_board: 'sandbox',
    expected_role: 'worker',
    seat_name: 'worker-one',
    tier_max: 2,
  });
  assert.equal(parsed.ok, true);
  assert.deepEqual(parsed.metadata, {
    board: 'sandbox',
    role: 'worker',
    kid: 'door-test-v1',
    exp: 2000000000,
    transport: 'http-loopback',
    remote: false,
  });
  assert.equal(validated.ok, true);
  assert.equal(validated.normalized.tier_max, 2);
  const rendered = JSON.stringify({ parsed, validated });
  assert.equal(rendered.includes(secretDoor), false);
  assert.equal(rendered.includes('127.0.0.1:8766'), false);
});

test('validate rejects expired, mismatched and insecure remote doors', () => {
  const onboarding = adapter({ runBridge: async () => '' });
  assert.equal(onboarding.validate({ door: door({ exp: 1800000000 }) }).code, 'expired_door');
  assert.equal(onboarding.validate({ door: door(), expected_board: 'other' }).code, 'wrong_board');
  assert.equal(onboarding.validate({ door: door(), expected_role: 'reviewer' }).code, 'wrong_role');
  assert.equal(onboarding.validate({ door: door({ url: 'http://central.example/mcp' }) }).code, 'insecure_remote_url');
  assert.equal(onboarding.validate({ door: door({ url: 'http://127.attacker.example/mcp' }) }).code, 'insecure_remote_url');
  assert.equal(onboarding.validate({ door: door({ url: 'http://127.42.7.9:8766/mcp' }) }).ok, true);
  assert.equal(onboarding.validate({ door: door({ url: 'http://worker.localhost:8766/mcp' }) }).ok, true);
  assert.equal(onboarding.validate({ door: door({ url: 'http://[::1]:8766/mcp' }) }).ok, true);
  assert.equal(onboarding.validate({ door: door(), tier_max: 4 }).code, 'invalid_tier');
  assert.equal(onboarding.validate({ door: door(), folder: '../shared' }).code, 'invalid_folder');
});

test('connect passes explicit identity and tier through the shipped bridge and Team fragment', async () => {
  const calls = [];
  let imported;
  const secretDoor = door({ url: 'https://central.example/mcp' });
  const onboarding = adapter({
    runBridge: async (args, options) => {
      calls.push({ args: [...args], options });
      if (args[0] === 'join') return 'board=sandbox\nrole=worker\nseat_name=worker-one\npush=yes\nverifier=accepted\n';
      return calls.length === 1
        ? 'push_mode=push\n'
        : 'push_mode=push\nboard=sandbox role=worker kid=door-test-v1 exp=2000000000 seat_names_used=worker-one\n';
    },
    importMcp: async (server) => { imported = server; return { success: true }; },
  });
  const connected = await onboarding.connect({
    door: secretDoor,
    seat_name: 'worker-one',
    assistant_id: 'bare:worker',
    model: 'provider/model',
    folder: 'worker-one',
    tier_max: 2,
  });
  assert.equal(connected.outcome, 'connected');
  assert.deepEqual(calls[1].args.slice(0, -1), ['join', '--allow-remote', '--name', 'worker-one']);
  assert.equal(calls[1].args.at(-1), secretDoor);
  assert.deepEqual(calls[1].options.env, { PURSERS_TIER_MAX: '2' });
  assert.deepEqual(connected.team, {
    outcome: 'ready',
    detail: 'Seat fragment is ready for TeamSpec.seats[].',
    seat: {
      name: 'worker-one',
      role: 'worker',
      tier_max: 2,
      folder: 'worker-one',
      assistant_id: 'bare:worker',
      model: 'provider/model',
    },
  });
  assert.deepEqual(imported.transport, { type: 'stdio', command: 'pursers-wait-bridge', args: [], env: {} });
  assert.equal(JSON.stringify(connected).includes(secretDoor), false);
});

test('connect recovers an existing identity without replaying the door', async () => {
  const calls = [];
  const onboarding = adapter({
    runBridge: async (args) => {
      calls.push([...args]);
      return 'push_mode=push\nboard=sandbox role=worker kid=door-test-v1 exp=2000000000 seat_names_used=worker-one\n';
    },
  });
  const connected = await onboarding.connect({ door: door(), tier_max: 1 });
  assert.equal(connected.outcome, 'recovered');
  assert.deepEqual(calls, [['status']]);
  assert.equal(connected.status.seat_name, 'worker-one');
  assert.equal(connected.team.seat.tier_max, 1);
});

test('rotation requires stored state and uses bridge rotate for a new key', async () => {
  const absent = adapter({ runBridge: async () => 'push_mode=push\n' });
  assert.equal((await absent.rotate({ door: door({ kid: 'door-test-v2' }) })).code, 'rotation_requires_existing');

  const calls = [];
  const rotating = adapter({
    runBridge: async (args) => {
      calls.push([...args]);
      if (args[0] === 'join') return 'board=sandbox\nrole=worker\nseat_name=worker-one\npush=yes\n';
      return calls.length === 1
        ? 'push_mode=push\nboard=sandbox role=worker kid=door-test-v1 exp=2000000000 seat_names_used=worker-one\n'
        : 'push_mode=push\nboard=sandbox role=worker kid=door-test-v2 exp=2000000000 seat_names_used=worker-one\n';
    },
  });
  const rotated = await rotating.rotate({ door: door({ kid: 'door-test-v2' }), seat_name: 'worker-one' });
  assert.equal(rotated.outcome, 'rotated');
  assert.deepEqual(calls[1].slice(0, -1), ['join', '--rotate', '--name', 'worker-one']);
});

test('partial MCP registration is typed and recoverable without a door', async () => {
  let importCalls = 0;
  const onboarding = adapter({
    runBridge: async (args) => args[0] === 'join'
      ? 'board=sandbox\nrole=worker\nseat_name=worker-one\npush=yes\n'
      : 'push_mode=push\nboard=sandbox role=worker kid=door-test-v1 exp=2000000000 seat_names_used=worker-one\n',
    importMcp: async () => ({ success: ++importCalls > 1 }),
  });
  const partial = await onboarding.connect({ door: door(), seat_name: 'worker-one' });
  assert.equal(partial.ok, false);
  assert.equal(partial.outcome, 'partial');
  assert.equal(partial.code, 'mcp_registration_failed');
  assert.equal(partial.connected, true);
  const recovered = await onboarding.recover({ board: 'sandbox', role: 'worker', seat_name: 'worker-one', tier_max: 2 });
  assert.equal(recovered.outcome, 'recovered');
  assert.equal(importCalls, 2);
  assert.equal((await onboarding.recover({ board: 'sandbox', role: 'worker', seat_name: 'worker-one', folder: '../shared' })).code, 'invalid_folder');
});

test('bridge failures are bounded and never echo the door', async () => {
  const secretDoor = door();
  const onboarding = adapter({
    runBridge: async (args) => {
      if (args[0] === 'status') return 'push_mode=push\n';
      const error = new Error(`failed ${secretDoor}`);
      error.stderr = 'connection refused';
      throw error;
    },
  });
  const failed = await onboarding.connect({ door: secretDoor, seat_name: 'worker-one' });
  assert.equal(failed.code, 'server_unreachable');
  assert.equal(failed.retryable, true);
  assert.equal(JSON.stringify(failed).includes(secretDoor), false);
});

test('connect fails closed when stored status cannot be read', async () => {
  const onboarding = adapter({
    runBridge: async () => {
      const error = new Error('state failure');
      error.stderr = 'invalid stored state';
      throw error;
    },
  });
  const failed = await onboarding.connect({ door: door(), seat_name: 'worker-one' });
  assert.equal(failed.code, 'bridge_status_failed');
  assert.equal(failed.retryable, true);
});
