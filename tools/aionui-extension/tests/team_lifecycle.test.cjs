'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { createTeamLifecycleAdapter } = require('../team_lifecycle/adapter.cjs');
const { createHandlers } = require('../webui/routes.js');

test('adapter pins board and validates create update remove inputs', async () => {
  const calls = [];
  const groups = createTeamLifecycleAdapter({
    expectedBoard: 'board-a',
    run: async (operation, payload) => { calls.push({ operation, payload }); return { ok: true, revision: payload.expected_revision }; },
  });
  assert.equal((await groups.create({ board: 'board-b' })).error.code, 'board_mismatch');
  assert.equal((await groups.create({ expected_revision: 0, name: '', member_agent_ids: ['AI-a'] })).error.code, 'invalid_input');
  assert.equal((await groups.create({ expected_revision: 0, name: 'Delivery', member_agent_ids: ['AI-a', 'AI-a'] })).error.code, 'invalid_input');
  assert.equal((await groups.update({ expected_revision: 1, group_id: 'bad', name: 'X', member_agent_ids: ['AI-a'] })).error.code, 'invalid_input');
  assert.equal((await groups.remove({ expected_revision: -1, group_id: 'group-a1b2c3d4e5f6' })).error.code, 'invalid_input');
  await groups.status();
  await groups.list();
  await groups.create({ expected_revision: 0, name: 'Delivery', member_agent_ids: ['AI-a'] });
  await groups.update({ expected_revision: 1, group_id: 'group-a1b2c3d4e5f6', name: 'Delivery 2', member_agent_ids: ['AI-b'] });
  await groups.remove({ expected_revision: 2, group_id: 'group-a1b2c3d4e5f6' });
  assert.deepEqual(calls.map((call) => call.operation), ['status', 'list', 'create', 'update', 'remove']);
  assert.deepEqual(calls[2].payload, { board: 'board-a', expected_revision: 0, name: 'Delivery', member_agent_ids: ['AI-a'] });
});

test('adapter maps transport and invalid backend failures', async () => {
  const broken = createTeamLifecycleAdapter({ expectedBoard: 'board-a', run: async () => { throw Object.assign(new Error('gone'), { code: 'backend_unavailable' }); } });
  assert.equal((await broken.list()).error.code, 'backend_unavailable');
  const malformed = createTeamLifecycleAdapter({ expectedBoard: 'board-a', run: async () => ({ surprise: true }) });
  assert.equal((await malformed.status()).error.code, 'backend_unavailable');
});

test('group routes are selected-board pinned and map typed status codes', async () => {
  const calls = [];
  const handlers = createHandlers({
    expectedBoard: 'board-a',
    runTeamLifecycle: async (operation, payload) => {
      calls.push({ operation, payload });
      if (operation === 'remove') return { ok: false, error: { code: 'conflict', message: 'stale' } };
      return { ok: true, board: 'board-a', revision: 0, groups: [], agents: [] };
    },
  });
  let response = await handlers.handle(new Request('http://127.0.0.1/pursers/groups', { headers: { origin: 'http://127.0.0.1' } }));
  assert.equal(response.status, 200);
  response = await handlers.handle(new Request('http://127.0.0.1/pursers/groups/create', {
    method: 'POST', headers: { origin: 'http://127.0.0.1', 'content-type': 'application/json' },
    body: JSON.stringify({ board: 'board-b', expected_revision: 0, name: 'X', member_agent_ids: ['AI-a'] }),
  }));
  assert.equal(response.status, 422);
  assert.equal((await response.json()).error.code, 'board_mismatch');
  response = await handlers.handle(new Request('http://127.0.0.1/pursers/groups/remove', {
    method: 'POST', headers: { origin: 'http://127.0.0.1', 'content-type': 'application/json' },
    body: JSON.stringify({ expected_revision: 0, group_id: 'group-a1b2c3d4e5f6' }),
  }));
  assert.equal(response.status, 409);
  assert.deepEqual(calls, [
    { operation: 'list', payload: { board: 'board-a' } },
    { operation: 'remove', payload: { board: 'board-a', expected_revision: 0, group_id: 'group-a1b2c3d4e5f6' } },
  ]);
});
