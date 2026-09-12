'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const {
  MAX_BODY_BYTES,
  TOKEN_HEADER,
  bridgeArguments,
  createHelperServer,
  createStandaloneTeamStatus,
  createTeamRunner,
  explicitRuntimeContext,
  normalizeOrigin,
  readTokenFile,
} = require('../host/helper.cjs');

const ORIGIN = 'http://127.0.0.1:25808';
const TOKEN = 'a'.repeat(64);

test('explicit issuer runtime is complete, private, and replaces ambient context', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'pursers-runtime-'));
  const tokenFile = path.join(directory, 'runtime-token');
  const command = path.join(directory, 'probe.cjs');
  fs.writeFileSync(tokenFile, 'r'.repeat(64), { mode: 0o600 });
  fs.writeFileSync(command, `#!/usr/bin/env node
process.stdin.resume();
process.stdin.on('end', () => process.stdout.write(JSON.stringify({ success: true, env: {
  base: process.env.AIONUI_BASE_URL || null,
  user: process.env.AIONUI_USER_ID || null,
  conversation: process.env.AIONUI_CONVERSATION_ID || null,
  token: process.env.AIONUI_RUNTIME_TOKEN || null,
}})));
`, { mode: 0o700 });
  assert.throws(
    () => explicitRuntimeContext({ 'runtime-base-url': ORIGIN }),
    /explicit Team runtime requires/,
  );
  const context = explicitRuntimeContext({
    'runtime-base-url': ORIGIN,
    'runtime-user-id': 'issuer-user',
    'runtime-conversation-id': 'issuer-conversation',
    'runtime-token-file': tokenFile,
  });
  assert.deepEqual(context, {
    AIONUI_BASE_URL: ORIGIN,
    AIONUI_USER_ID: 'issuer-user',
    AIONUI_CONVERSATION_ID: 'issuer-conversation',
    AIONUI_RUNTIME_TOKEN: 'r'.repeat(64),
  });
  const previous = Object.fromEntries(['AIONUI_BASE_URL', 'AIONUI_USER_ID', 'AIONUI_CONVERSATION_ID', 'AIONUI_RUNTIME_TOKEN'].map((key) => [key, process.env[key]]));
  Object.assign(process.env, {
    AIONUI_BASE_URL: 'http://127.0.0.1:9999',
    AIONUI_USER_ID: 'borrowed-user',
    AIONUI_CONVERSATION_ID: 'borrowed-conversation',
    AIONUI_RUNTIME_TOKEN: 'borrowed-token',
  });
  try {
    assert.deepEqual((await createTeamRunner(command)(['members'], {})).env, {
      base: null, user: null, conversation: null, token: null,
    });
    assert.deepEqual((await createTeamRunner(command, context)(['members'], {})).env, {
      base: ORIGIN,
      user: 'issuer-user',
      conversation: 'issuer-conversation',
      token: 'r'.repeat(64),
    });
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    fs.rmSync(directory, { recursive: true, force: true });
  }
});

function segment(value) {
  return Buffer.from(JSON.stringify(value)).toString('base64url');
}

function door(board) {
  const token = `${segment({ alg: 'RS256', kid: 'synthetic-key' })}.${segment({ exp: 2000000000 })}.synthetic-signature`;
  return `prs1.${segment({ u: 'http://127.0.0.1:8766/mcp', b: board, r: 'worker', t: token })}`;
}

async function runningHelper(overrides = {}) {
  const teamCalls = [];
  const helper = createHelperServer({
    board: 'sandbox-home',
    central: 'work',
    origin: ORIGIN,
    token: TOKEN,
    port: 0,
    coreVersion: '0.2.1',
    runBridge: async (args) => {
      assert.deepEqual(args, ['status']);
      return [
        'push_mode=push',
        'board=sandbox-home role=worker kid=synthetic-key exp=2000000000 seat_names_used=worker-1',
        'board=other-board role=reviewer kid=other-key exp=2000000001 seat_names_used=reviewer-1',
      ].join('\n');
    },
    runTeamCli: async (command) => {
      teamCalls.push(command);
      return {
        success: false,
        error: { code: 'runtime_context_missing', message: 'Team runtime context is unavailable.' },
        meta: { schema_version: 1 },
      };
    },
    ...overrides,
  });
  const address = await helper.start();
  return { helper, teamCalls, baseUrl: `http://127.0.0.1:${address.port}` };
}

function authHeaders(token = TOKEN, origin = ORIGIN) {
  return { origin, [TOKEN_HEADER]: token };
}

test('helper authenticates one exact origin and exposes selected-board status only', async () => {
  const { helper, baseUrl } = await runningHelper();
  try {
    const metadata = await fetch(`${baseUrl}/pursers/helper/status`, { headers: authHeaders() });
    assert.equal(metadata.status, 200);
    assert.deepEqual(await metadata.json(), {
      ok: true,
      board: 'sandbox-home',
      central: 'work',
      transport: 'authenticated_loopback_helper',
      host_route_handlers: false,
      team_context: 'unavailable_from_settings_tab',
      team_status_source: 'board_managed_standalone',
      core_version: '0.2.1',
    });

    const status = await fetch(`${baseUrl}/pursers/onboarding/status`, { headers: authHeaders() });
    assert.equal(status.status, 200);
    assert.deepEqual(await status.json(), {
      ok: true,
      operation: 'status',
      outcome: 'ready',
      push_mode: 'push',
      seats: [{
        board: 'sandbox-home',
        role: 'worker',
        kid: 'synthetic-key',
        exp: 2000000000,
        seat_names: ['worker-1'],
      }],
    });

    const wrongToken = await fetch(`${baseUrl}/pursers/helper/status`, { headers: authHeaders('b'.repeat(64)) });
    assert.equal(wrongToken.status, 401);
    assert.equal(JSON.stringify(await wrongToken.json()).includes(TOKEN), false);

    const wrongOrigin = await fetch(`${baseUrl}/pursers/helper/status`, { headers: authHeaders(TOKEN, 'http://localhost:25808') });
    assert.equal(wrongOrigin.status, 403);
    assert.equal(wrongOrigin.headers.get('access-control-allow-origin'), null);
  } finally {
    await helper.close();
  }
});

test('standalone Team status exposes only the bounded board lifecycle projection', async () => {
  const calls = [];
  const status = createStandaloneTeamStatus(async (operation, payload) => {
    calls.push([operation, payload]);
    return {
      ok: true,
      board: 'sandbox-home',
      revision: 3,
      groups: [{ group_id: 'group-a1b2c3d4e5f6' }],
      agents: [{
        agent_id: 'AI-worker-1', agent_name: 'worker-1', role: 'worker',
        lifecycle_status: 'active', principal_id: 'must-not-leak', slot_id: 'must-not-leak',
      }],
    };
  }, 'sandbox-home');
  const payload = await status();
  assert.deepEqual(calls, [['list', { board: 'sandbox-home' }]]);
  assert.deepEqual(payload, {
    ok: true,
    op: 'status',
    mode: 'board_managed_standalone',
    native_team: false,
    board: 'sandbox-home',
    group_revision: 3,
    group_count: 1,
    members: [{
      agent_id: 'AI-worker-1', name: 'worker-1', role: 'worker',
      status: 'active', lifecycle_status: 'active',
    }],
    tasks: [],
  });
  assert.equal(JSON.stringify(payload).includes('must-not-leak'), false);
});

test('helper Team status uses standalone board lifecycle while native mutations stay closed', async () => {
  const groupProcess = {
    async run(operation, payload) {
      assert.deepEqual([operation, payload], ['list', { board: 'sandbox-home' }]);
      return {
        ok: true, board: 'sandbox-home', revision: 0, groups: [],
        agents: [{ agent_id: 'AI-worker-1', agent_name: 'worker-1', role: 'worker', lifecycle_status: 'active' }],
      };
    },
    async close() {},
  };
  const { helper, teamCalls, baseUrl } = await runningHelper({ groupProcess });
  try {
    const response = await fetch(`${baseUrl}/pursers/team/status`, { headers: authHeaders() });
    const payload = await response.json();
    assert.equal(response.status, 200);
    assert.equal(payload.mode, 'board_managed_standalone');
    assert.equal(payload.native_team, false);
    assert.equal(payload.members[0].name, 'worker-1');
    assert.deepEqual(teamCalls, []);
  } finally {
    await helper.close();
  }
});

test('standalone Team status fails closed on invalid board identity data', async () => {
  const status = createStandaloneTeamStatus(async () => ({
    ok: true, board: 'sandbox-home', revision: 0, groups: [],
    agents: [{ agent_id: 'AI-worker-1', agent_name: '../worker', role: 'worker', lifecycle_status: 'active' }],
  }), 'sandbox-home');
  const payload = await status();
  assert.equal(payload.ok, false);
  assert.equal(payload.error.code, 'transport_unavailable');
});

test('helper handles CORS preflight and refuses a door for another board', async () => {
  const { helper, baseUrl } = await runningHelper();
  try {
    const preflight = await fetch(`${baseUrl}/pursers/onboarding/validate`, {
      method: 'OPTIONS',
      headers: {
        origin: ORIGIN,
        'access-control-request-method': 'POST',
        'access-control-request-headers': `content-type,${TOKEN_HEADER}`,
      },
    });
    assert.equal(preflight.status, 204);
    assert.equal(preflight.headers.get('access-control-allow-origin'), ORIGIN);

    const response = await fetch(`${baseUrl}/pursers/onboarding/validate`, {
      method: 'POST',
      headers: { ...authHeaders(), 'content-type': 'application/json' },
      body: JSON.stringify({ door: door('other-board'), expected_role: 'worker' }),
    });
    const payload = await response.json();
    assert.equal(response.status, 422);
    assert.equal(payload.code, 'wrong_board');
    assert.equal(JSON.stringify(payload).includes(door('other-board')), false);
  } finally {
    await helper.close();
  }
});

test('helper keeps Team apply closed when the settings page has no runtime context', async () => {
  const { helper, teamCalls, baseUrl } = await runningHelper();
  try {
    const response = await fetch(`${baseUrl}/pursers/team/apply`, {
      method: 'POST',
      headers: { ...authHeaders(), 'content-type': 'application/json' },
      body: JSON.stringify({
        team: { name: 'Sandbox' },
        lead: { name: 'lead', assistant_id: 'lead-assistant' },
        seats: [{ name: 'worker-1', assistant_id: 'worker-assistant', role: 'worker', tier_max: 2, folder: 'worker-1' }],
        options: { confirm: 'apply-live', dry_run: false, send_kickoff: true },
      }),
    });
    const payload = await response.json();
    assert.equal(response.status, 409);
    assert.equal(payload.error.code, 'runtime_context_missing');
    assert.deepEqual(teamCalls, [['members']]);
  } finally {
    await helper.close();
  }
});

test('helper returns a bounded error for an oversized request body', async () => {
  const { helper, baseUrl } = await runningHelper();
  try {
    const response = await fetch(`${baseUrl}/pursers/onboarding/validate`, {
      method: 'POST',
      headers: { ...authHeaders(), 'content-type': 'application/json' },
      body: JSON.stringify({ value: 'x'.repeat(MAX_BODY_BYTES + 1) }),
    });
    assert.equal(response.status, 413);
    assert.deepEqual(await response.json(), { ok: false, error: 'request_too_large' });
  } finally {
    await helper.close();
  }
});

test('helper authenticates standalone lifecycle and supplies retirement trust internally', async () => {
  const calls = [];
  let current = {
    board: 'sandbox-home', agent_id: 'AI-worker-1', principal_id: 'PR-worker',
    agent_name: 'worker-1', role: 'worker', lifecycle_status: 'active', status: 'idle', lease_expires_at: null,
  };
  const seatProcess = {
    async run(operation, payload) {
      calls.push([operation, payload]);
      if (operation === 'join') return { ok: true, identity: current, rejoined: false };
      if (operation === 'read') return { ok: true, board_id: 'sandbox-home', agents: [current] };
      if (operation === 'retire') {
        current = { ...current, lifecycle_status: 'retired' };
        return { ok: true, board_id: 'sandbox-home', agent: current };
      }
      return { ok: true, board: 'sandbox-home', role: 'worker' };
    },
    async close() {},
  };
  const { helper, baseUrl } = await runningHelper({ seatProcess });
  try {
    const denied = await fetch(`${baseUrl}/pursers/seat-lifecycle/status`, { headers: authHeaders('b'.repeat(64)) });
    assert.equal(denied.status, 401);
    const joined = await fetch(`${baseUrl}/pursers/seat-lifecycle/join`, {
      method: 'POST', headers: { ...authHeaders(), 'content-type': 'application/json' },
      body: JSON.stringify({ board: 'sandbox-home', door: 'not-returned', agent_name: 'worker-1', role: 'worker' }),
    });
    assert.equal(joined.status, 200);
    const retired = await fetch(`${baseUrl}/pursers/seat-lifecycle/disconnect`, {
      method: 'POST', headers: { ...authHeaders(), 'content-type': 'application/json' },
      body: JSON.stringify({ board: 'sandbox-home', confirm: 'retire worker-1 from sandbox-home' }),
    });
    assert.equal(retired.status, 200);
    const retire = calls.find(([operation]) => operation === 'retire');
    assert.deepEqual(retire[1].expected_identity, {
      board: 'sandbox-home', agent_id: 'AI-worker-1', principal_id: 'PR-worker',
      agent_name: 'worker-1', role: 'worker',
    });
    assert.equal(JSON.stringify(await retired.json()).includes('not-returned'), false);
  } finally {
    await helper.close();
  }
});

test('token files and helper arguments are bounded', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'pursers-home-helper-'));
  const tokenFile = path.join(directory, 'token');
  fs.writeFileSync(tokenFile, `${TOKEN}\n`, { mode: 0o600 });
  assert.equal(readTokenFile(tokenFile), TOKEN);
  fs.chmodSync(tokenFile, 0o644);
  assert.throws(() => readTokenFile(tokenFile), /group or other users/);
  assert.equal(normalizeOrigin('http://127.0.0.1:25808'), ORIGIN);
  assert.throws(() => normalizeOrigin('http://127.attacker.example:25808'), /loopback/);
  assert.throws(() => createHelperServer({ board: 'sandbox-home' }), /central/);
  assert.deepEqual(bridgeArguments(['status'], '/isolated/state'), ['status', '--state-dir', '/isolated/state']);
  assert.deepEqual(
    bridgeArguments(['join', '--name', 'worker-1', 'door-value'], '/isolated/state'),
    ['join', '--name', 'worker-1', '--state-dir', '/isolated/state', 'door-value'],
  );
});
