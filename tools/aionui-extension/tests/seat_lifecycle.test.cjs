'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const { createSeatLifecycle } = require('../seat_lifecycle/adapter.cjs');

const BOARD = 'sandbox-home';

function identity(overrides = {}) {
  return {
    board: BOARD,
    agent_id: 'AI-worker-1',
    principal_id: 'PR-worker',
    agent_name: 'worker-1',
    role: 'worker',
    lifecycle_status: 'active',
    ...overrides,
  };
}

function harness(overrides = {}) {
  const calls = [];
  let agent = identity();
  const dependencies = {
    expectedBoard: BOARD,
    joinSeat: async (input) => {
      calls.push(['join', input]);
      agent = identity();
      return { ok: true, identity: agent, rejoined: true };
    },
    readBoard: async (input) => {
      calls.push(['status', input]);
      return { ok: true, board_id: BOARD, agents: [{ ...agent, status: 'idle', lease_expires_at: null }] };
    },
    retireSelf: async (input) => {
      calls.push(['retire', input]);
      agent = identity({ lifecycle_status: 'retired' });
      return { ok: true, board_id: BOARD, changed: true, agent };
    },
    forgetDoor: async (input) => {
      calls.push(['forget', input]);
      return { ok: true, ...input };
    },
    ...overrides,
  };
  return {
    calls,
    lifecycle: createSeatLifecycle(dependencies),
    setAgent(value) { agent = value; },
  };
}

test('join preserves and verifies the exact Central identity', async () => {
  const { lifecycle, calls } = harness();
  const joined = await lifecycle.join({
    board: BOARD,
    agent_name: 'worker-1',
    role: 'worker',
    door: 'secret-not-returned',
    untrusted_extra: 'not-forwarded',
  });

  assert.equal(joined.ok, true);
  assert.equal(joined.outcome, 'joined');
  assert.deepEqual(joined.identity, identity());
  assert.equal(JSON.stringify(joined).includes('secret-not-returned'), false);
  assert.deepEqual(calls.map(([name]) => name), ['join', 'status']);
  assert.deepEqual(calls[0][1], {
    board: BOARD,
    agent_name: 'worker-1',
    role: 'worker',
    expected_board: BOARD,
    door: 'secret-not-returned',
  });
  assert.deepEqual(calls[1][1], { board: BOARD, include_retired: true });
});

test('join rejects another board before calling a dependency', async () => {
  const { lifecycle, calls } = harness();
  const joined = await lifecycle.join({ board: 'other-board', agent_name: 'worker-1', role: 'worker' });
  assert.equal(joined.code, 'wrong_board');
  assert.deepEqual(calls, []);
});

test('bound rejoin supplies the exact preserved identity to the dependency', async () => {
  const { lifecycle, calls } = harness({ initialIdentity: identity() });
  const joined = await lifecycle.join({
    board: BOARD,
    agent_name: 'worker-1',
    role: 'worker',
    agent_id: 'AI-worker-1',
    principal_id: 'PR-worker',
  });
  assert.equal(joined.ok, true);
  assert.deepEqual(calls[0], ['join', {
    board: BOARD,
    agent_name: 'worker-1',
    role: 'worker',
    expected_board: BOARD,
    expected_identity: {
      board: BOARD,
      agent_id: 'AI-worker-1',
      principal_id: 'PR-worker',
      agent_name: 'worker-1',
      role: 'worker',
    },
  }]);
});

for (const [field, value] of [
  ['expected_board', BOARD],
  ['expected_identity', { agent_id: 'AI-attacker', principal_id: 'PR-attacker' }],
  ['agent_id', 'AI-attacker'],
  ['principal_id', 'PR-attacker'],
]) {
  test(`fresh join rejects caller trust selector ${field} before mutation`, async () => {
    const { lifecycle, calls } = harness();
    const joined = await lifecycle.join({
      board: BOARD,
      agent_name: 'worker-1',
      role: 'worker',
      [field]: value,
    });
    assert.equal(joined.code, 'reserved_identity_selector');
    assert.deepEqual(calls, []);
  });
}

for (const field of ['expected_board', 'expected_identity']) {
  test(`bound rejoin rejects caller internal selector ${field} before mutation`, async () => {
    const { lifecycle, calls } = harness({ initialIdentity: identity() });
    const joined = await lifecycle.join({
      board: BOARD,
      agent_name: 'worker-1',
      role: 'worker',
      [field]: field === 'expected_board' ? BOARD : identity(),
    });
    assert.equal(joined.code, 'reserved_identity_selector');
    assert.deepEqual(calls, []);
  });
}

for (const [field, value] of [
  ['board', 'other-board'],
  ['agent_name', 'worker-2'],
  ['role', 'reviewer'],
  ['agent_id', 'AI-worker-2'],
  ['principal_id', 'PR-other'],
]) {
  test(`bound rejoin rejects a different ${field} before any dependency mutation`, async () => {
    const { lifecycle, calls } = harness({ initialIdentity: identity() });
    const input = { board: BOARD, agent_name: 'worker-1', role: 'worker', [field]: value };
    const joined = await lifecycle.join(input);
    assert.equal(joined.ok, false);
    assert.ok(['wrong_board', 'identity_mismatch'].includes(joined.code));
    assert.deepEqual(calls, []);
  });
}

test('join rejects ok false even when the dependency returns a valid-looking identity', async () => {
  const { lifecycle, calls } = harness({
    joinSeat: async (input) => {
      calls.push(['join', input]);
      return { ok: false, identity: identity(), rejoined: true };
    },
  });
  const joined = await lifecycle.join({ board: BOARD, agent_name: 'worker-1', role: 'worker' });
  assert.equal(joined.ok, false);
  assert.equal(joined.outcome, 'failed');
  assert.equal(joined.code, 'join_failed');
  assert.match(joined.recovery, /same seat name/);
  assert.deepEqual(calls.map(([name]) => name), ['join']);
});

test('status restores one exact identity and reports recovery guidance', async () => {
  const { lifecycle } = harness();
  const status = await lifecycle.status({ board: BOARD, agent_name: 'worker-1', role: 'worker' });
  assert.equal(status.ok, true);
  assert.equal(status.outcome, 'active');
  assert.equal(status.confirmation, 'retire worker-1 from sandbox-home');
  assert.deepEqual(status.identity, identity());
});

for (const lifecycleStatus of ['handed_off', 'stale']) {
  test(`status reports ${lifecycleStatus} without claiming the seat is active`, async () => {
    const { lifecycle, calls, setAgent } = harness({ initialIdentity: identity() });
    const row = identity({ lifecycle_status: lifecycleStatus });
    setAgent(row);
    const status = await lifecycle.status();
    assert.equal(status.ok, false);
    assert.equal(status.outcome, lifecycleStatus);
    assert.equal(status.code, `seat_${lifecycleStatus}`);
    assert.equal(status.identity.lifecycle_status, lifecycleStatus);
    assert.match(status.recovery, /rejoin/i);
    assert.notEqual(status.outcome, 'active');
    const disconnected = await lifecycle.disconnect({ confirm: 'retire worker-1 from sandbox-home' });
    assert.equal(disconnected.outcome, lifecycleStatus);
    assert.deepEqual(calls.map(([name]) => name), ['status', 'status']);
  });
}

test('status reports literal unknown as a bounded non-active lifecycle', async () => {
  const { lifecycle, calls, setAgent } = harness();
  setAgent(identity({ lifecycle_status: 'unknown' }));
  const status = await lifecycle.status({ board: BOARD, agent_name: 'worker-1', role: 'worker' });
  assert.equal(status.ok, false);
  assert.equal(status.outcome, 'unknown');
  assert.equal(status.code, 'seat_unknown');
  assert.equal(status.retryable, true);
  assert.match(status.recovery, /rejoin/i);
  const disconnected = await lifecycle.disconnect({ confirm: 'retire worker-1 from sandbox-home' });
  assert.equal(disconnected.code, 'seat_unknown');
  assert.deepEqual(calls.map(([name]) => name), ['status', 'status']);
});

for (const [label, lifecycleStatus] of [['missing', undefined], ['malformed', 'mystery']]) {
  test(`status rejects ${label} dependency lifecycle data rather than classifying it unknown`, async () => {
    const row = identity();
    if (lifecycleStatus === undefined) delete row.lifecycle_status;
    else row.lifecycle_status = lifecycleStatus;
    const { lifecycle } = harness({
      initialIdentity: identity(),
      readBoard: async () => ({ ok: true, board_id: BOARD, agents: [row] }),
    });
    const status = await lifecycle.status();
    assert.equal(status.ok, false);
    assert.equal(status.outcome, 'failed');
    assert.equal(status.code, 'invalid_board_response');
    assert.notEqual(status.code, 'seat_unknown');
  });
}

test('status fails closed for duplicate or malformed board identity', async () => {
  const duplicate = identity({ agent_id: 'AI-worker-2', principal_id: 'PR-other' });
  const { lifecycle } = harness({
    readBoard: async () => ({ ok: true, board_id: BOARD, agents: [identity(), duplicate] }),
  });
  const status = await lifecycle.status({ agent_name: 'worker-1', role: 'worker' });
  assert.equal(status.code, 'identity_conflict');

  const wrongBoard = harness({
    readBoard: async () => ({ ok: true, board_id: 'other-board', agents: [identity()] }),
  });
  assert.equal((await wrongBoard.lifecycle.status({ agent_name: 'worker-1', role: 'worker' })).code, 'invalid_board_response');
});

test('a bound identity cannot silently change principal or agent ID', async () => {
  const { lifecycle, setAgent } = harness({ initialIdentity: identity() });
  setAgent(identity({ principal_id: 'PR-replacement' }));
  const status = await lifecycle.status();
  assert.equal(status.code, 'identity_mismatch');
  assert.match(status.recovery, /Do not retire/);
});

test('disconnect requires the exact consequential confirmation', async () => {
  const { lifecycle, calls } = harness({ initialIdentity: identity() });
  const disconnected = await lifecycle.disconnect({ confirm: 'retire worker-2 from sandbox-home' });
  assert.equal(disconnected.code, 'confirmation_required');
  assert.equal(disconnected.message, 'Type exactly: retire worker-1 from sandbox-home');
  assert.deepEqual(calls.map(([name]) => name), ['status']);
});

test('disconnect refuses an active work or review lease', async () => {
  const { lifecycle, calls } = harness({
    initialIdentity: identity(),
    readBoard: async (input) => {
      calls.push(['status', input]);
      return { ok: true, board_id: BOARD, agents: [{ ...identity(), lease_expires_at: '2030-01-01T00:00:00Z' }] };
    },
  });
  const disconnected = await lifecycle.disconnect({ confirm: 'retire worker-1 from sandbox-home' });
  assert.equal(disconnected.code, 'active_lease');
  assert.equal(disconnected.retryable, true);
  assert.deepEqual(calls.map(([name]) => name), ['status']);
});

test('disconnect self-retires, verifies Central, then forgets local state', async () => {
  const { lifecycle, calls } = harness({ initialIdentity: identity() });
  const disconnected = await lifecycle.disconnect({ confirm: 'retire worker-1 from sandbox-home' });
  assert.equal(disconnected.ok, true);
  assert.equal(disconnected.outcome, 'disconnected');
  assert.equal(disconnected.identity.lifecycle_status, 'retired');
  assert.deepEqual(calls, [
    ['status', { board: BOARD, include_retired: true }],
    ['retire', { board: BOARD, agent_name: 'worker-1' }],
    ['status', { board: BOARD, include_retired: true }],
    ['forget', { board: BOARD, role: 'worker' }],
  ]);
});

test('disconnect never forgets state after an identity-swapped retirement response', async () => {
  const { lifecycle, calls } = harness({
    initialIdentity: identity(),
    retireSelf: async (input) => {
      calls.push(['retire', input]);
      return { ok: true, board_id: BOARD, agent: identity({ agent_id: 'AI-other', lifecycle_status: 'retired' }) };
    },
  });
  const disconnected = await lifecycle.disconnect({ confirm: 'retire worker-1 from sandbox-home' });
  assert.equal(disconnected.code, 'invalid_retirement_response');
  assert.deepEqual(calls.map(([name]) => name), ['status', 'retire']);
});

test('disconnect keeps local state until retirement read-back is verified', async () => {
  const { lifecycle, calls } = harness({
    initialIdentity: identity(),
    retireSelf: async (input) => {
      calls.push(['retire', input]);
      return { ok: true, board_id: BOARD, agent: identity({ lifecycle_status: 'retired' }) };
    },
  });
  const disconnected = await lifecycle.disconnect({ confirm: 'retire worker-1 from sandbox-home' });
  assert.equal(disconnected.code, 'retirement_not_verified');
  assert.deepEqual(calls.map(([name]) => name), ['status', 'retire', 'status']);
});

test('disconnect reports a recoverable partial result if local forget fails', async () => {
  const { lifecycle } = harness({
    initialIdentity: identity(),
    forgetDoor: async () => { throw new Error('synthetic secret path failure'); },
  });
  const disconnected = await lifecycle.disconnect({ confirm: 'retire worker-1 from sandbox-home' });
  assert.equal(disconnected.ok, false);
  assert.equal(disconnected.outcome, 'partial');
  assert.equal(disconnected.code, 'local_disconnect_incomplete');
  assert.equal(JSON.stringify(disconnected).includes('synthetic secret path failure'), false);
});

test('constructor rejects missing dependencies and cross-board initial identity', () => {
  assert.throws(() => createSeatLifecycle({ expectedBoard: BOARD }), /joinSeat dependency/);
  assert.throws(() => harness({ initialIdentity: identity({ board: 'other-board' }) }), /another board/);
});

test('assembly contract assigns exact authenticated routes and dependency interfaces', () => {
  const contract = fs.readFileSync(path.join(__dirname, '../seat_lifecycle/FEATURE_CONTRACT.md'), 'utf8');
  for (const required of [
    'TK-a3f0627d27db',
    'POST /pursers/seat-lifecycle/join',
    'GET /pursers/seat-lifecycle/status',
    'POST /pursers/seat-lifecycle/disconnect',
    'x-pursers-home-token',
    'expected_identity',
    '`expected_board` and `expected_identity` are adapter-owned fields',
    'readBoard({ board, include_retired: true })',
    'retireSelf({ board, agent_name })',
    'forgetDoor({ board, role })',
    'confirmation-required or non-active lifecycle results to 422',
  ]) assert.ok(contract.includes(required), `missing assembly contract: ${required}`);
});
