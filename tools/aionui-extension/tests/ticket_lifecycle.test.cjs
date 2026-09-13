'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
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

function loadClaimUi(overrides = {}) {
  const source = fs.readFileSync(path.join(__dirname, '..', 'webui', 'app.js'), 'utf8');
  const start = source.indexOf('function ticketOffer(ticket)');
  const end = source.indexOf('\nasync function loadTickets()', start);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  return vm.runInNewContext(`${source.slice(start, end)}\n({ ticketOffer, claimOffer })`, {
    errorCode: (result) => result.code || result.error?.code || result.error || 'unknown_error',
    messageFor: (result, fallback) => result.message || result.error?.message || fallback,
    setBusy: () => {},
    setMessage: (target, message) => { target.textContent = message; },
    ticketMessage: {},
    ...overrides,
  });
}

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
  assert.equal((await lifecycle.claim({ ticket_id: 'TK-1', agent_name: '../other' })).error.code, 'invalid_input');
  assert.equal(calls, 0);
});

test('persisted read, create, claim, and cancel routes exist; submission and review stay unavailable', async () => {
  const calls = [];
  const handlers = createHandlers({
    expectedBoard: 'demo',
    runTicketLifecycle: async (operation, payload) => {
      calls.push({ operation, payload });
      if (operation === 'list') return { ok: true, board: 'demo', tickets: [{ ticket_id: 'TK-1', status: 'submitted' }] };
      if (operation === 'claim') return {
        ok: true,
        board: 'demo',
        ticket: { ticket_id: payload.ticket_id, status: 'claimed', claimed_by: payload.agent_name },
        identity: { agent_id: 'AI-worker', principal_id: 'PR-worker', agent_name: payload.agent_name, role: 'worker' },
      };
      return { ok: true, board: 'demo', ticket: { ticket_id: payload.ticket_id || 'TK-2', status: operation === 'cancel' ? 'canceled' : 'open' } };
    },
  });
  const list = await handlers.handle(new Request('http://127.0.0.1/pursers/tickets'));
  const create = await handlers.handle(new Request('http://127.0.0.1/pursers/tickets/create', { method: 'POST', body: JSON.stringify(valid) }));
  const claim = await handlers.handle(new Request('http://127.0.0.1/pursers/tickets/claim', { method: 'POST', body: JSON.stringify({ ticket_id: 'TK-2', agent_name: 'worker-one' }) }));
  const cancel = await handlers.handle(new Request('http://127.0.0.1/pursers/tickets/cancel', { method: 'POST', body: JSON.stringify({ ticket_id: 'TK-2' }) }));
  const submit = await handlers.handle(new Request('http://127.0.0.1/pursers/tickets/submit', { method: 'POST', body: '{}' }));
  const review = await handlers.handle(new Request('http://127.0.0.1/pursers/tickets/review', { method: 'POST', body: '{}' }));
  assert.equal(list.status, 200);
  assert.equal(create.status, 200);
  assert.equal(claim.status, 200);
  assert.equal((await claim.json()).identity.agent_name, 'worker-one');
  assert.equal(cancel.status, 200);
  assert.equal(submit.status, 404);
  assert.equal(review.status, 404);
  assert.deepEqual(calls.map((entry) => entry.operation), ['list', 'create', 'claim', 'cancel']);
});

test('claim rendering never turns a route 404 into expiry and preserves a real Central refusal', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'webui', 'app.js'), 'utf8');
  const start = source.indexOf('function claimFailure(result)');
  const end = source.indexOf('\nasync function loadTickets()', start);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  const claimFailure = vm.runInNewContext(`${source.slice(start, end)}\nclaimFailure`, {
    errorCode: (result) => result.code || result.error?.code || result.error || 'unknown_error',
    messageFor: (result, fallback) => result.message || result.error?.message || fallback,
  });
  const missing = claimFailure({ ok: false, error: 'not_found' });
  assert.equal(missing.code, 'not_found');
  assert.doesNotMatch(missing.message, /expired|Central refused/i);
  const refusal = claimFailure({
    ok: false,
    error: {
      code: 'claim_refused',
      message: 'ticket is not offered to this seat; wait for your offer',
      retryable: false,
    },
  });
  assert.deepEqual({ ...refusal }, {
    code: 'claim_refused',
    message: 'ticket is not offered to this seat; wait for your offer',
  });
});

test('UI claim path sends work_offer.agent_name through the exact adapter', async () => {
  const backendCalls = [];
  const lifecycle = createTicketLifecycleAdapter({
    expectedBoard: 'demo',
    run: async (operation, payload) => {
      backendCalls.push({ operation, payload });
      return {
        ok: true,
        board: 'demo',
        ticket: { ticket_id: payload.ticket_id, status: 'claimed', claimed_by: payload.agent_name },
        identity: { agent_name: payload.agent_name },
      };
    },
  });
  const row = { dataset: {}, error: { dataset: {} }, identity: { dataset: {} } };
  const button = { closest: () => row };
  let refreshes = 0;
  const { ticketOffer, claimOffer } = loadClaimUi({
    $: (selector) => selector === '.claim-error' ? row.error : row.identity,
    api: async (endpoint, options) => {
      assert.equal(endpoint, '/pursers/tickets/claim');
      const result = await lifecycle.claim(options.json);
      return { response: { ok: result.ok }, result };
    },
    loadTickets: async () => { refreshes += 1; },
  });
  const ticket = {
    ticket_id: 'TK-live',
    status: 'open',
    work_offer: {
      agent_name: 'worker-one',
      offered_at: '2026-09-13T08:00:00Z',
      expires_at: '2026-09-13T09:00:00Z',
    },
  };
  assert.equal(ticketOffer(ticket).agent_name, 'worker-one');
  await claimOffer(ticket, button);
  assert.deepEqual(backendCalls, [{
    operation: 'claim',
    payload: { board: 'demo', ticket_id: 'TK-live', agent_name: 'worker-one' },
  }]);
  assert.equal(row.dataset.offerStatus, 'claimed');
  assert.equal(row.identity.dataset.claimedIdentity, 'worker-one');
  assert.equal(refreshes, 1);
});

test('UI expired history offer reaches adapter and preserves Central refusal unchanged', async () => {
  const refusal = {
    ok: false,
    error: {
      code: 'claim_refused',
      message: 'ticket is not offered to this seat; wait for your offer',
      retryable: false,
    },
  };
  const backendCalls = [];
  const lifecycle = createTicketLifecycleAdapter({
    expectedBoard: 'demo',
    run: async (operation, payload) => {
      backendCalls.push({ operation, payload });
      return refusal;
    },
  });
  const row = { dataset: {}, error: { dataset: {} }, identity: { dataset: {} } };
  const { ticketOffer, claimOffer } = loadClaimUi({
    $: (selector) => selector === '.claim-error' ? row.error : row.identity,
    api: async (_endpoint, options) => {
      const result = await lifecycle.claim(options.json);
      assert.equal(result, refusal);
      return { response: { ok: false }, result };
    },
    loadTickets: async () => { throw new Error('refused claims must not refresh'); },
  });
  const ticket = {
    ticket_id: 'TK-expired',
    status: 'open',
    dispatch_history: [{
      state: 'expired',
      agent_name: 'worker-one',
      offered_at: '2026-09-13T07:00:00Z',
      expires_at: '2026-09-13T07:10:00Z',
    }],
  };
  assert.equal(ticketOffer(ticket).agent_name, 'worker-one');
  await claimOffer(ticket, { closest: () => row });
  assert.deepEqual(backendCalls, [{
    operation: 'claim',
    payload: { board: 'demo', ticket_id: 'TK-expired', agent_name: 'worker-one' },
  }]);
  assert.equal(row.error.dataset.claimError, 'claim_refused');
  assert.equal(row.error.textContent, refusal.error.message);
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

test('helper token and exact origin protect board-pinned ticket creation and claim', async () => {
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
        if (operation === 'claim' && payload.ticket_id === 'TK-expired') return {
          ok: false,
          error: {
            code: 'claim_refused',
            message: 'ticket is not offered to this seat; wait for your offer',
            retryable: false,
          },
        };
        if (operation === 'claim') return {
          ok: true,
          board: 'demo',
          ticket: { ticket_id: payload.ticket_id, status: 'claimed', claimed_by: payload.agent_name },
          identity: { agent_id: 'AI-worker', principal_id: 'PR-worker', agent_name: payload.agent_name, role: 'worker' },
        };
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
    const claimOptions = (ticketId) => ({
      ...options(token, origin),
      body: JSON.stringify({ ticket_id: ticketId, agent_name: 'worker-one' }),
    });
    const claimed = await fetch(`http://127.0.0.1:${address.port}/pursers/tickets/claim`, claimOptions('TK-live'));
    assert.equal(claimed.status, 200);
    assert.equal((await claimed.json()).identity.agent_name, 'worker-one');
    const refused = await fetch(`http://127.0.0.1:${address.port}/pursers/tickets/claim`, claimOptions('TK-expired'));
    assert.equal(refused.status, 409);
    assert.deepEqual(await refused.json(), {
      ok: false,
      error: {
        code: 'claim_refused',
        message: 'ticket is not offered to this seat; wait for your offer',
        retryable: false,
      },
    });
    assert.deepEqual(calls.map((call) => call.operation), ['create', 'claim', 'claim']);
    assert.deepEqual(calls.map((call) => call.payload.board), ['demo', 'demo', 'demo']);
  } finally {
    await helper.close();
  }
  assert.equal(closed, true);
});
