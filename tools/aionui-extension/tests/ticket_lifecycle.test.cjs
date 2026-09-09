'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { createTicketLifecycleAdapter } = require('../ticket_lifecycle/adapter.cjs');
const { createHandlers } = require('../webui/routes.js');
const { createHelperServer, TOKEN_HEADER } = require('../host/helper.cjs');

const valid = {
  title: 'Add a real feature',
  description: 'Persist this ticket on Central.',
  target_url: 'home/feature',
  scope: 'interactive-no-send',
  required_fields: ['branch_and_commit'],
  priority: 'high',
  tier: 2,
};

test('create is board-pinned, unassigned by backend contract, and preserves supported fields', async () => {
  const calls = [];
  const lifecycle = createTicketLifecycleAdapter({
    expectedBoard: 'demo',
    run: async (operation, payload) => {
      calls.push({ operation, payload });
      return { ok: true, board: 'demo', ticket: { ticket_id: 'TK-real', status: 'open', assigned_to: null } };
    },
  });
  const result = await lifecycle.create({ ...valid, board: 'demo' });
  assert.equal(result.ticket.status, 'open');
  assert.deepEqual(calls, [{ operation: 'create', payload: { board: 'demo', ...valid } }]);
});

test('adapter rejects wrong boards and unsupported input before backend mutation', async () => {
  let calls = 0;
  const lifecycle = createTicketLifecycleAdapter({
    expectedBoard: 'demo',
    run: async () => { calls += 1; return { ok: true }; },
  });
  assert.equal((await lifecycle.create({ ...valid, board: 'other' })).error.code, 'board_mismatch');
  assert.equal((await lifecycle.create({ ...valid, tier: 4 })).error.code, 'invalid_input');
  assert.equal(calls, 0);
});

test('only persisted read, create, and cancel routes exist; submission and review stay unavailable', async () => {
  const calls = [];
  const handlers = createHandlers({
    expectedBoard: 'demo',
    runTicketLifecycle: async (operation, payload) => {
      calls.push({ operation, payload });
      if (operation === 'list') return { ok: true, board: 'demo', tickets: [{ ticket_id: 'TK-1', status: 'submitted' }] };
      return { ok: true, board: 'demo', ticket: { ticket_id: payload.ticket_id || 'TK-2', status: operation === 'cancel' ? 'canceled' : 'open' } };
    },
  });
  const list = await handlers.handle(new Request('http://127.0.0.1/pursers/tickets'));
  const create = await handlers.handle(new Request('http://127.0.0.1/pursers/tickets/create', { method: 'POST', body: JSON.stringify(valid) }));
  const cancel = await handlers.handle(new Request('http://127.0.0.1/pursers/tickets/cancel', { method: 'POST', body: JSON.stringify({ ticket_id: 'TK-2' }) }));
  const submit = await handlers.handle(new Request('http://127.0.0.1/pursers/tickets/submit', { method: 'POST', body: '{}' }));
  const review = await handlers.handle(new Request('http://127.0.0.1/pursers/tickets/review', { method: 'POST', body: '{}' }));
  assert.equal(list.status, 200);
  assert.equal(create.status, 200);
  assert.equal(cancel.status, 200);
  assert.equal(submit.status, 404);
  assert.equal(review.status, 404);
  assert.deepEqual(calls.map((entry) => entry.operation), ['list', 'create', 'cancel']);
});

test('ticket routes enforce loopback origin and surface recovery status', async () => {
  const handlers = createHandlers({
    expectedBoard: 'demo',
    allowedOrigin: 'http://127.0.0.1:25808',
    runTicketLifecycle: async () => ({
      ok: false,
      error: { code: 'backend_unavailable', message: 'Restart or reconnect the helper, then retry.', retryable: true },
    }),
  });
  const wrongOrigin = await handlers.handle(new Request('http://127.0.0.1/pursers/tickets', { headers: { origin: 'http://localhost:25808' } }));
  const recovery = await handlers.handle(new Request('http://127.0.0.1/pursers/tickets', { headers: { origin: 'http://127.0.0.1:25808' } }));
  assert.equal(wrongOrigin.status, 403);
  assert.equal(recovery.status, 503);
  assert.equal((await recovery.json()).error.retryable, true);
});

test('helper token and exact origin protect board-pinned ticket creation', async () => {
  const calls = [];
  let closed = false;
  const token = 'a'.repeat(64);
  const origin = 'http://127.0.0.1:25808';
  const helper = createHelperServer({
    board: 'demo', central: 'local-central', origin, token, port: 0,
    runBridge: async () => 'push_mode=push\n',
    runTeamCli: async () => ({ success: false, error: { code: 'runtime_context_missing', message: 'Unavailable.' } }),
    ticketLifecycle: {
      run: async (operation, payload) => {
        calls.push({ operation, payload });
        return { ok: true, board: 'demo', ticket: { ticket_id: 'TK-real', status: 'open' } };
      },
      close: async () => { closed = true; },
    },
  });
  const address = await helper.start();
  const url = `http://127.0.0.1:${address.port}/pursers/tickets/create`;
  const options = (selectedToken, selectedOrigin) => ({
    method: 'POST',
    headers: { origin: selectedOrigin, [TOKEN_HEADER]: selectedToken, 'content-type': 'application/json' },
    body: JSON.stringify({ ...valid, board: 'demo' }),
  });
  try {
    assert.equal((await fetch(url, options('b'.repeat(64), origin))).status, 401);
    assert.equal((await fetch(url, options(token, 'http://localhost:25808'))).status, 403);
    const created = await fetch(url, options(token, origin));
    assert.equal(created.status, 200);
    assert.equal((await created.json()).ticket.ticket_id, 'TK-real');
    assert.deepEqual(calls.map((call) => call.payload.board), ['demo']);
  } finally {
    await helper.close();
  }
  assert.equal(closed, true);
});
