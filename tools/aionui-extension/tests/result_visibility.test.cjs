'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const {
  MAX_RESULTS,
  createResultVisibility,
  normalizeTicket,
} = require('../result_visibility/adapter.cjs');
const {
  MAX_FLEET_BODY_BYTES,
  TOKEN_HEADER,
  createFleetResultsFetcher,
  createHelperServer,
} = require('../host/helper.cjs');

const ORIGIN = 'http://127.0.0.1:25808';
const TOKEN = 'a'.repeat(64);
const CENTRAL = 'work';

function ticket(id, state = 'pending') {
  return {
    id,
    title: `Ticket ${id}`,
    status: state === 'approved' ? 'closed' : 'submitted',
    updated_at: '2030-01-01T12:00:00+00:00',
    result: {
      state,
      summary: 'Implemented safely',
      branch: `codex/${id}`,
      commit: '0123456789abcdef0123456789abcdef01234567',
      files_changed: ['tools/feature.js'],
      files_omitted: 0,
      submitted_at: '2030-01-01T10:00:00+00:00',
      review: state === 'approved'
        ? {
          verdict: 'approve',
          reviewer: 'reviewer-1',
          reviewed_at: '2030-01-01T11:00:00+00:00',
          independent: true,
        }
        : {},
    },
  };
}

test('lists bounded persisted result states for the pinned board', async () => {
  const feature = createResultVisibility({
    expectedBoard: 'sandbox-home',
    expectedCentral: CENTRAL,
    fetchBoard: async (board, central) => ({
      central,
      board: { board_id: board },
      generated_at: '2030-01-01T12:00:00+00:00',
      tickets: [ticket('TK-pending'), ticket('TK-approved', 'approved')],
      truncated: false,
    }),
  });

  const value = await feature.read();
  assert.equal(value.ok, true);
  assert.equal(value.board, 'sandbox-home');
  assert.deepEqual(value.results.map((item) => item.result_state), ['pending', 'approved']);
  assert.equal(value.results[1].review.independent, true);
});

test('filters one ticket and reports honest missing state', async () => {
  const feature = createResultVisibility({
    expectedBoard: 'sandbox-home',
    expectedCentral: CENTRAL,
    fetchBoard: async () => ({
      central: CENTRAL,
      board: { board_id: 'sandbox-home' },
      tickets: [ticket('TK-one', 'missing')],
    }),
  });

  assert.equal((await feature.read({ ticketId: 'TK-one' })).result.result_state, 'missing');
  assert.deepEqual(
    await feature.read({ ticketId: 'TK-absent' }),
    { ok: false, code: 'ticket_not_found', retryable: false },
  );
});

test('refuses wrong board and maps backend failure without leaking detail', async () => {
  const wrongBoard = createResultVisibility({
    expectedBoard: 'sandbox-home',
    expectedCentral: CENTRAL,
    fetchBoard: async () => ({ central: CENTRAL, board: { board_id: 'production' }, tickets: [] }),
  });
  const failed = createResultVisibility({
    expectedBoard: 'sandbox-home',
    expectedCentral: CENTRAL,
    fetchBoard: async () => {
      throw new Error('secret backend detail');
    },
  });

  assert.deepEqual(
    await wrongBoard.read(),
    { ok: false, code: 'invalid_backend_response', retryable: true },
  );
  assert.deepEqual(
    await failed.read(),
    { ok: false, code: 'backend_unavailable', retryable: true },
  );
});

test('rejects invalid filters before calling the backend', async () => {
  let calls = 0;
  const feature = createResultVisibility({
    expectedBoard: 'sandbox-home',
    expectedCentral: CENTRAL,
    fetchBoard: async () => {
      calls += 1;
      return { central: CENTRAL, board: { board_id: 'sandbox-home' }, tickets: [] };
    },
  });

  assert.equal((await feature.read({ ticketId: '../other' })).code, 'invalid_ticket_id');
  assert.equal((await feature.read({ state: 'complete' })).code, 'invalid_result_state');
  assert.equal(calls, 0);
});

test('bounds rows and strips unsafe artifact fields', async () => {
  const unsafe = ticket('TK-unsafe', 'rejected');
  unsafe.result.branch = '../../escape';
  unsafe.result.files_changed = ['/absolute', '../escape', 'safe/file.txt'];
  unsafe.result.files_omitted = Number.MAX_SAFE_INTEGER;
  unsafe.result.review = {
    verdict: 'reject',
    reviewer: 'reviewer-1',
    reviewed_at: '2030-01-01T11:00:00+00:00',
    independent: true,
  };
  const rows = Array.from({ length: MAX_RESULTS + 5 }, (_, index) => ticket(`TK-${index}`));
  const feature = createResultVisibility({
    expectedBoard: 'sandbox-home',
    expectedCentral: CENTRAL,
    fetchBoard: async () => ({
      central: CENTRAL,
      board: { board_id: 'sandbox-home' },
      tickets: [unsafe, ...rows],
    }),
  });

  const projected = normalizeTicket(unsafe);
  assert.equal(projected.submission.branch, null);
  assert.equal(projected.submission.commit, null);
  assert.deepEqual(projected.submission.files_changed, ['safe/file.txt']);
  assert.equal(projected.submission.files_omitted, 10000);
  const value = await feature.read();
  assert.equal(value.returned, MAX_RESULTS);
  assert.equal(value.omitted, 6);
  assert.equal(value.truncated, true);
});

test('authenticated helper exposes only the configured board results', async () => {
  let calls = 0;
  const helper = createHelperServer({
    board: 'sandbox-home',
    central: CENTRAL,
    origin: ORIGIN,
    token: TOKEN,
    port: 0,
    runBridge: async () => '',
    runTeamCli: async () => ({ success: false }),
    fetchResults: async (board, central) => {
      calls += 1;
      return { central, board: { board_id: board }, tickets: [ticket('TK-one')] };
    },
  });
  const address = await helper.start();
  const baseUrl = `http://127.0.0.1:${address.port}`;
  try {
    const response = await fetch(`${baseUrl}/pursers/results?ticket_id=TK-one`, {
      headers: { origin: ORIGIN, [TOKEN_HEADER]: TOKEN },
    });
    assert.equal(response.status, 200);
    assert.equal((await response.json()).result.ticket_id, 'TK-one');
    assert.equal(calls, 1);

    const wrongToken = await fetch(`${baseUrl}/pursers/results`, {
      headers: { origin: ORIGIN, [TOKEN_HEADER]: 'b'.repeat(64) },
    });
    assert.equal(wrongToken.status, 401);
    const wrongOrigin = await fetch(`${baseUrl}/pursers/results`, {
      headers: { origin: 'http://localhost:25808', [TOKEN_HEADER]: TOKEN },
    });
    assert.equal(wrongOrigin.status, 403);
    assert.equal(calls, 1);
  } finally {
    await helper.close();
  }
});

test('Fleet fetcher is loopback-only and forwards no credentials', async () => {
  const calls = [];
  const fetchBoard = createFleetResultsFetcher(
    'http://127.0.0.1:8899',
    async (url, options) => {
      calls.push({ url, options });
      return new Response(JSON.stringify({
        board: { board_id: 'sandbox-home' },
        tickets: [],
      }));
    },
  );

  await fetchBoard('sandbox-home', CENTRAL);
  assert.equal(calls[0].url, 'http://127.0.0.1:8899/api/board/sandbox-home?central=work');
  assert.deepEqual(calls[0].options.headers, { accept: 'application/json' });
  assert.equal(calls[0].options.credentials, 'omit');
  assert.throws(
    () => createFleetResultsFetcher('https://fleet.example'),
    /loopback/,
  );
});

test('Central and board are both pinned when duplicate board IDs exist', async () => {
  const domains = {
    work: { central: 'work', board: { board_id: 'shared' }, tickets: [ticket('TK-work')] },
    personal: { central: 'personal', board: { board_id: 'shared' }, tickets: [ticket('TK-personal')] },
  };
  const feature = createResultVisibility({
    expectedBoard: 'shared',
    expectedCentral: 'personal',
    fetchBoard: async (board, central) => {
      assert.equal(board, 'shared');
      return domains[central];
    },
  });

  const value = await feature.read();
  assert.equal(value.central, 'personal');
  assert.deepEqual(value.results.map((item) => item.ticket_id), ['TK-personal']);

  const wrongDomain = createResultVisibility({
    expectedBoard: 'shared',
    expectedCentral: 'personal',
    fetchBoard: async () => domains.work,
  });
  const missingDomain = createResultVisibility({
    expectedBoard: 'shared',
    expectedCentral: 'personal',
    fetchBoard: async () => ({ board: { board_id: 'shared' }, tickets: [] }),
  });
  assert.equal((await wrongDomain.read()).code, 'invalid_backend_response');
  assert.equal((await missingDomain.read()).code, 'invalid_backend_response');
});

test('Fleet fetcher cancels an oversized stream despite absent or false length', async () => {
  for (const declared of [null, '0']) {
    let reads = 0;
    let cancelled = false;
    const fetchBoard = createFleetResultsFetcher('http://127.0.0.1:8899', async () => ({
      ok: true,
      headers: new Headers(declared === null ? {} : { 'content-length': declared }),
      body: {
        getReader() {
          return {
            async read() {
              reads += 1;
              return { done: false, value: new Uint8Array(reads === 1 ? MAX_FLEET_BODY_BYTES : 1) };
            },
            async cancel() { cancelled = true; },
          };
        },
      },
    }));

    await assert.rejects(fetchBoard('shared', 'work'), /too large/);
    assert.equal(reads, 2);
    assert.equal(cancelled, true);
  }
});

test('Fleet fetcher cancels declared oversized bodies before reading', async () => {
  let reads = 0;
  let cancelled = false;
  const fetchBoard = createFleetResultsFetcher('http://127.0.0.1:8899', async () => ({
    ok: true,
    headers: new Headers({ 'content-length': String(MAX_FLEET_BODY_BYTES + 1) }),
    body: {
      async cancel() { cancelled = true; },
      getReader() {
        return { async read() { reads += 1; return { done: true }; } };
      },
    },
  }));

  await assert.rejects(fetchBoard('shared', 'work'), /too large/);
  assert.equal(reads, 0);
  assert.equal(cancelled, true);
});
