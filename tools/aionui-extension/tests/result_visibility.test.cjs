'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const {
  MAX_RESULTS,
  createResultVisibility,
  normalizeTicket,
} = require('../result_visibility/adapter.cjs');
const {
  TOKEN_HEADER,
  createFleetResultsFetcher,
  createHelperServer,
} = require('../host/helper.cjs');

const ORIGIN = 'http://127.0.0.1:25808';
const TOKEN = 'a'.repeat(64);

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
    fetchBoard: async (board) => ({
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
    fetchBoard: async () => ({
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
    fetchBoard: async () => ({ board: { board_id: 'production' }, tickets: [] }),
  });
  const failed = createResultVisibility({
    expectedBoard: 'sandbox-home',
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
    fetchBoard: async () => {
      calls += 1;
      return { board: { board_id: 'sandbox-home' }, tickets: [] };
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
    fetchBoard: async () => ({
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
    origin: ORIGIN,
    token: TOKEN,
    port: 0,
    runBridge: async () => '',
    runTeamCli: async () => ({ success: false }),
    fetchResults: async (board) => {
      calls += 1;
      return { board: { board_id: board }, tickets: [ticket('TK-one')] };
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

  await fetchBoard('sandbox-home');
  assert.equal(calls[0].url, 'http://127.0.0.1:8899/api/board/sandbox-home');
  assert.deepEqual(calls[0].options.headers, { accept: 'application/json' });
  assert.equal(calls[0].options.credentials, 'omit');
  assert.throws(
    () => createFleetResultsFetcher('https://fleet.example'),
    /loopback/,
  );
});
