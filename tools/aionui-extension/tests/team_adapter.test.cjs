'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const {
  CONTRACT,
  CONTRACT_SCHEMA_VERSION,
  OUTCOMES,
  HOST_ERROR_CODES,
  createTeamAdapter,
  validateTeamSpec,
  buildSeatKickoff,
  buildLeadBrief,
} = require('../team/adapter.cjs');

const CATALOG = [
  { assistant_id: 'bare:600c6601', name: 'Goose', backend: 'goose', description: '', skills: [] },
  { assistant_id: 'bare:8e1acf31', name: 'Codex CLI', backend: 'codex', description: '', skills: [] },
];

const LEAD = {
  slot_id: 'slot-lead', name: 'Goose', role: 'lead', status: 'idle',
  assistant_id: 'bare:600c6601', model: 'vertex_ai/gemini-3.8-flash',
};

function makeSpec(overrides) {
  return Object.assign({
    team: { name: 'Demo-Worker-Team' },
    lead: { name: 'Goose', assistant_id: 'bare:600c6601', monitor_only: true },
    seats: [
      { name: 'pursers-demo-goose-1', assistant_id: 'bare:600c6601', role: 'worker', tier_max: 2, folder: 'pursers-demo-goose-1' },
      { name: 'pursers-demo-goose-2', assistant_id: 'bare:600c6601', model: 'glm-5.2', role: 'worker', tier_max: 1, folder: 'pursers-demo-goose-2' },
    ],
  }, overrides);
}

function envelope(data) {
  return { success: true, data, meta: { schema_version: 1 } };
}

function errorEnvelope(code, message) {
  return { success: false, error: { code, message: message || code }, meta: { schema_version: 1 } };
}

function fakeHost(members, options = {}) {
  const roster = members.slice();
  const calls = [];
  const overrides = options.overrides || {};
  const runCli = async (command, input) => {
    const payload = JSON.parse(JSON.stringify(input === undefined ? {} : input));
    calls.push({ command: command.slice(), input: payload });
    const key = command.join(' ');
    if (Object.prototype.hasOwnProperty.call(overrides, key)) {
      const value = overrides[key];
      return typeof value === 'function' ? value(payload, calls) : value;
    }
    switch (key) {
      case 'members': return envelope({ members: roster.slice() });
      case 'list-assistants': return envelope({ assistants: CATALOG });
      case 'spawn-agent': {
        const slot = `slot-${payload.name}`;
        roster.push({ slot_id: slot, name: payload.name, role: 'teammate', status: 'idle', assistant_id: payload.assistant_id, model: null });
        return envelope({ slot_id: slot });
      }
      case 'send-message': return envelope({ message_id: `msg-${calls.length}` });
      case 'task list': return envelope({ tasks: [] });
      case 'task create':
      case 'interrupt-agent':
      case 'shutdown-agent':
      case 'rename-agent':
      case 'clear-agent-context':
        return envelope({});
      default: return errorEnvelope('unknown_tool', `no fake for "${key}"`);
    }
  };
  return { runCli, calls, roster };
}

function commands(calls) {
  return calls.map((entry) => entry.command.join(' '));
}

test('contract identity is pinned to the installed host contract', () => {
  assert.equal(CONTRACT, 'agent-facing-team-cli');
  assert.equal(CONTRACT_SCHEMA_VERSION, 1);
  assert.deepEqual(HOST_ERROR_CODES, [
    'unknown_tool', 'schema_validation_failed', 'permission_denied', 'team_not_found',
    'conversation_not_found', 'agent_not_found', 'not_in_team', 'transport_unavailable',
    'runtime_context_missing', 'runtime_auth_failed',
  ]);
});

test('validateTeamSpec accepts a minimal spec and rejects malformed ones', () => {
  assert.deepEqual(validateTeamSpec(makeSpec()), { ok: true, errors: [] });
  const bad = validateTeamSpec({
    team: {}, lead: { name: 'x' },
    seats: [
      { name: 'a', assistant_id: 'bare:1', role: 'boss', tier_max: 9, folder: '../escape' },
      { name: 'a', assistant_id: 'bare:1', role: 'worker', tier_max: 2, folder: '/abs/path' },
    ],
    options: { confirm: 'yes-please', send_kickoff: 'sometimes' },
  });
  assert.equal(bad.ok, false);
  const joined = bad.errors.join('\n');
  for (const needle of ['/team/name', '/lead/assistant_id', '/seats/0/role', '/seats/0/tier_max', '/seats/0/folder', 'duplicate seat name', '/seats/1/folder', '/options/confirm', '/options/send_kickoff']) {
    assert.ok(joined.includes(needle), `missing validation error ${needle}`);
  }
});

test('plan diffs against roster and catalog without mutating', async () => {
  const host = fakeHost([LEAD]);
  const adapter = createTeamAdapter({ runCli: host.runCli });
  const planned = await adapter.plan(makeSpec());
  assert.equal(planned.ok, true);
  assert.deepEqual(commands(host.calls), ['members', 'list-assistants']);
  assert.deepEqual(host.calls[0].input, {});
  assert.deepEqual(host.calls[1].input, {});
  const seatResults = planned.results.filter((entry) => entry.action === 'spawn');
  assert.equal(seatResults.length, 2);
  assert.ok(seatResults.every((entry) => entry.outcome === OUTCOMES.OK));
  assert.equal(planned.results.find((entry) => entry.action === 'lead-check').outcome, OUTCOMES.OK);
});

test('plan flags missing or mismatched lead', async () => {
  const noLead = fakeHost([]);
  const adapter = createTeamAdapter({ runCli: noLead.runCli });
  let planned = await adapter.plan(makeSpec());
  assert.equal(planned.results.find((e) => e.action === 'lead-check').detail, 'lead_not_found: roster has no member with role "lead"');

  const wrongLead = fakeHost([Object.assign({}, LEAD, { name: 'Someone Else' })]);
  const adapter2 = createTeamAdapter({ runCli: wrongLead.runCli });
  planned = await adapter2.plan(makeSpec());
  assert.ok(planned.results.find((e) => e.action === 'lead-check').detail.startsWith('lead_name_mismatch'));
});

test('plan is idempotent: matching roster members become skipped_exists', async () => {
  const host = fakeHost([
    LEAD,
    { slot_id: 'slot-a', name: 'pursers-demo-goose-1', role: 'teammate', status: 'working', assistant_id: 'bare:600c6601', model: null },
    { slot_id: 'slot-b', name: 'pursers-demo-goose-2', role: 'teammate', status: 'idle', assistant_id: 'bare:600c6601', model: 'glm-5.2' },
  ]);
  const adapter = createTeamAdapter({ runCli: host.runCli });
  const planned = await adapter.plan(makeSpec());
  const reused = planned.results.filter((entry) => entry.action === 'reuse');
  assert.equal(reused.length, 2);
  assert.ok(reused.every((entry) => entry.outcome === OUTCOMES.SKIPPED_EXISTS));
  assert.deepEqual(reused.map((entry) => entry.slot_id), ['slot-a', 'slot-b']);
});

test('identity conflict: same name, different assistant_id or model — zero mutations', async () => {
  const host = fakeHost([
    LEAD,
    { slot_id: 'slot-x', name: 'pursers-demo-goose-1', role: 'teammate', status: 'working', assistant_id: 'bare:8e1acf31', model: 'gpt-5.6-sol' },
    { slot_id: 'slot-y', name: 'pursers-demo-goose-2', role: 'teammate', status: 'idle', assistant_id: 'bare:600c6601', model: 'other/model' },
  ]);
  const adapter = createTeamAdapter({ runCli: host.runCli });
  const applied = await adapter.apply(Object.assign(makeSpec(), { options: { confirm: 'apply-live', dry_run: false } }));
  const conflicts = applied.results.filter((entry) => entry.outcome === OUTCOMES.IDENTITY_CONFLICT);
  assert.equal(conflicts.length, 2);
  assert.ok(conflicts[0].detail.includes('assistant_id differs'));
  assert.ok(conflicts[1].detail.includes('model differs'));
  assert.ok(!commands(host.calls).includes('spawn-agent'));
  assert.ok(!commands(host.calls).includes('send-message'));
  assert.ok(!commands(host.calls).includes('rename-agent'));
  assert.ok(!commands(host.calls).includes('shutdown-agent'));
});

test('apply defaults to dry-run and calls no mutating tool', async () => {
  const host = fakeHost([LEAD]);
  const adapter = createTeamAdapter({ runCli: host.runCli });
  const applied = await adapter.apply(makeSpec());
  assert.equal(applied.dry_run, true);
  const spawns = applied.results.filter((entry) => entry.action === 'spawn');
  assert.equal(spawns.length, 2);
  assert.ok(spawns.every((entry) => entry.outcome === OUTCOMES.DRY_RUN));
  assert.deepEqual(commands(host.calls).filter((name) => name !== 'members' && name !== 'list-assistants'), []);
});

test('live apply sends byte-exact documented payloads and kickoffs', async () => {
  const host = fakeHost([LEAD]);
  const adapter = createTeamAdapter({ runCli: host.runCli });
  const applied = await adapter.apply(Object.assign(makeSpec(), { options: { confirm: 'apply-live', dry_run: false } }));
  assert.equal(applied.dry_run, false);
  const spawns = host.calls.filter((entry) => entry.command.join(' ') === 'spawn-agent');
  assert.equal(spawns.length, 2);
  assert.deepEqual(spawns[0].input, { name: 'pursers-demo-goose-1', assistant_id: 'bare:600c6601' });
  assert.deepEqual(spawns[1].input, { name: 'pursers-demo-goose-2', assistant_id: 'bare:600c6601' });
  const kickoffs = host.calls.filter((entry) => entry.command.join(' ') === 'send-message');
  assert.equal(kickoffs.length, 2);
  assert.equal(kickoffs[0].input.to, 'slot-pursers-demo-goose-1');
  assert.ok(kickoffs[0].input.message.includes('pursers-demo-goose-1'));
  assert.ok(kickoffs[0].input.message.includes('tier_max: 2'));
  assert.ok(kickoffs[1].input.message.includes('tier_max: 1'));
  assert.ok(kickoffs[1].input.message.includes('board-only'));
  assert.equal(applied.summary.ok, 5); // lead-check + 2 spawns + 2 kickoffs
  assert.equal(applied.summary.failed, 0);
});

test('live apply on existing seats skips spawn; send_kickoff "always" re-delivers', async () => {
  const host = fakeHost([
    LEAD,
    { slot_id: 'slot-a', name: 'pursers-demo-goose-1', role: 'teammate', status: 'idle', assistant_id: 'bare:600c6601', model: null },
    { slot_id: 'slot-b', name: 'pursers-demo-goose-2', role: 'teammate', status: 'idle', assistant_id: 'bare:600c6601', model: 'glm-5.2' },
  ]);
  const adapter = createTeamAdapter({ runCli: host.runCli });
  let applied = await adapter.apply(Object.assign(makeSpec(), { options: { confirm: 'apply-live', dry_run: false } }));
  assert.equal(applied.summary.skipped_exists, 2);
  assert.ok(!commands(host.calls).includes('spawn-agent'));
  assert.ok(!commands(host.calls).includes('send-message'));

  const host2 = fakeHost([
    LEAD,
    { slot_id: 'slot-a', name: 'pursers-demo-goose-1', role: 'teammate', status: 'idle', assistant_id: 'bare:600c6601', model: null },
    { slot_id: 'slot-b', name: 'pursers-demo-goose-2', role: 'teammate', status: 'idle', assistant_id: 'bare:600c6601', model: 'glm-5.2' },
  ]);
  const adapter2 = createTeamAdapter({ runCli: host2.runCli });
  applied = await adapter2.apply(Object.assign(makeSpec(), { options: { confirm: 'apply-live', dry_run: false, send_kickoff: 'always' } }));
  const kickoffs = host2.calls.filter((entry) => entry.command.join(' ') === 'send-message');
  assert.equal(kickoffs.length, 2);
  assert.deepEqual(kickoffs.map((entry) => entry.input.to), ['slot-a', 'slot-b']);
});

test('partial failure: one seat denied, the other still spawns', async () => {
  const host = fakeHost([LEAD], {
    overrides: {
      'spawn-agent': (payload) => (payload.name === 'pursers-demo-goose-1'
        ? errorEnvelope('permission_denied', 'lead_only tool called by a teammate')
        : envelope({ slot_id: `slot-${payload.name}` })),
    },
  });
  const adapter = createTeamAdapter({ runCli: host.runCli });
  const applied = await adapter.apply(Object.assign(makeSpec(), { options: { confirm: 'apply-live', dry_run: false } }));
  const failed = applied.results.find((entry) => entry.seat === 'pursers-demo-goose-1' && entry.action === 'spawn');
  assert.equal(failed.outcome, OUTCOMES.FAILED);
  assert.ok(failed.detail.startsWith('permission_denied'));
  const ok = applied.results.find((entry) => entry.seat === 'pursers-demo-goose-2' && entry.action === 'spawn');
  assert.equal(ok.outcome, OUTCOMES.OK);
  assert.equal(applied.summary.failed, 1);
});

test('spawn ack without slot_id falls back to roster re-read, else fails honestly', async () => {
  const withFallback = fakeHost([LEAD], {
    overrides: {
      'spawn-agent': (payload, calls) => {
        withFallback.roster.push({ slot_id: 'slot-from-roster', name: payload.name, role: 'teammate', status: 'idle', assistant_id: payload.assistant_id, model: null });
        return envelope({});
      },
    },
  });
  let adapter = createTeamAdapter({ runCli: withFallback.runCli });
  let applied = await adapter.apply(Object.assign(makeSpec({ seats: [makeSpec().seats[0]] }), { options: { confirm: 'apply-live', dry_run: false } }));
  const spawned = applied.results.find((entry) => entry.action === 'spawn');
  assert.equal(spawned.outcome, OUTCOMES.OK);
  assert.equal(spawned.slot_id, 'slot-from-roster');
  assert.ok(commands(withFallback.calls).filter((name) => name === 'members').length >= 2);

  const noFallback = fakeHost([LEAD], { overrides: { 'spawn-agent': envelope({}) } });
  adapter = createTeamAdapter({ runCli: noFallback.runCli });
  applied = await adapter.apply(Object.assign(makeSpec({ seats: [makeSpec().seats[0]] }), { options: { confirm: 'apply-live', dry_run: false } }));
  const failed = applied.results.find((entry) => entry.action === 'spawn');
  assert.equal(failed.outcome, OUTCOMES.FAILED);
  assert.ok(failed.detail.includes('spawn_ack_unparsed'));
});

test('runtime_context_missing surfaces as failed plan, never a guessed team', async () => {
  const host = fakeHost([LEAD], { overrides: { members: errorEnvelope('runtime_context_missing', 'AIONUI_CONVERSATION_ID not set') } });
  const adapter = createTeamAdapter({ runCli: host.runCli });
  const planned = await adapter.plan(makeSpec());
  assert.equal(planned.ok, false);
  assert.equal(planned.error.code, 'runtime_context_missing');
});

test('unknown assistant_id fails the seat before any spawn', async () => {
  const host = fakeHost([LEAD]);
  const adapter = createTeamAdapter({ runCli: host.runCli });
  const spec = makeSpec({ seats: [{ name: 'ghost', assistant_id: 'bare:does-not-exist', role: 'worker', tier_max: 2, folder: 'ghost' }] });
  const planned = await adapter.plan(spec);
  const ghost = planned.results.find((entry) => entry.seat === 'ghost');
  assert.equal(ghost.outcome, OUTCOMES.FAILED);
  assert.ok(ghost.detail.includes('assistant_not_found'));
});

test('pause and stop map to interrupt-agent / shutdown-agent with exact payloads', async () => {
  const host = fakeHost([LEAD]);
  const adapter = createTeamAdapter({ runCli: host.runCli });
  const paused = await adapter.pauseSeat('slot-a', 'hold until reviewer finishes', 'scope change');
  assert.equal(paused.results[0].outcome, OUTCOMES.OK);
  assert.deepEqual(host.calls[0].command, ['interrupt-agent']);
  assert.deepEqual(host.calls[0].input, { slot_id: 'slot-a', message: 'hold until reviewer finishes', reason: 'scope change' });

  const badPause = await adapter.pauseSeat('slot-a', '');
  assert.equal(badPause.results[0].outcome, OUTCOMES.FAILED);
  assert.equal(host.calls.length, 1);

  const stopped = await adapter.stopSeat('slot-a', 'operator teardown');
  assert.equal(stopped.results[0].outcome, OUTCOMES.OK);
  assert.ok(stopped.results[0].detail.includes('shutdown_rejected'));
  assert.deepEqual(host.calls[1].command, ['shutdown-agent']);
  assert.deepEqual(host.calls[1].input, { slot_id: 'slot-a', reason: 'operator teardown' });
  const stopNoReason = await adapter.stopSeat('slot-a');
  assert.deepEqual(host.calls[2].input, { slot_id: 'slot-a' });
  assert.equal(stopNoReason.results[0].outcome, OUTCOMES.OK);
});

test('unsupported host surfaces are reported, never fabricated', async () => {
  const host = fakeHost([LEAD]);
  const adapter = createTeamAdapter({ runCli: host.runCli });
  const removed = adapter.removeSeat('slot-a');
  assert.equal(removed.results[0].outcome, OUTCOMES.UNSUPPORTED_BY_HOST);
  assert.ok(removed.results[0].detail.includes('/api/teams/{id}/agents/{slot_id}'));
  assert.equal(adapter.createTeam().results[0].outcome, OUTCOMES.UNSUPPORTED_BY_HOST);
  assert.equal(adapter.archiveTeam().results[0].outcome, OUTCOMES.UNSUPPORTED_BY_HOST);
  assert.equal(host.calls.length, 0);
});

test('rename and context reset stay explicit lead-only operations', async () => {
  const host = fakeHost([LEAD]);
  const adapter = createTeamAdapter({ runCli: host.runCli });
  const renamed = await adapter.renameSeat('slot-a', 'pursers-demo-goose-9');
  assert.deepEqual(host.calls[0].input, { slot_id: 'slot-a', new_name: 'pursers-demo-goose-9' });
  assert.equal(renamed.results[0].outcome, OUTCOMES.OK);
  const reset = await adapter.resetSeatContext('slot-a');
  assert.deepEqual(host.calls[1].command, ['clear-agent-context']);
  assert.equal(reset.results[0].outcome, OUTCOMES.OK);
});

test('status passes through the live roster shape with counts', async () => {
  const host = fakeHost([LEAD, { slot_id: 'slot-a', name: 'pursers-demo-goose-1', role: 'teammate', status: 'working', assistant_id: 'bare:600c6601', model: null }]);
  const adapter = createTeamAdapter({ runCli: host.runCli });
  const reported = await adapter.status({ tasks: true });
  assert.equal(reported.ok, true);
  assert.deepEqual(reported.counts, { members: 2, lead: 1, teammate: 1 });
  assert.deepEqual(reported.members[0], LEAD);
  assert.ok(commands(host.calls).includes('task list'));
});

test('kickoff and lead brief carry role, tier, folder and invariants verbatim', () => {
  const spec = makeSpec();
  const kickoff = buildSeatKickoff(spec, spec.seats[1]);
  assert.ok(kickoff.includes('You are the Pursers seat named pursers-demo-goose-2 in AionUi Team "Demo-Worker-Team".'));
  assert.ok(kickoff.includes('"pursers-demo-goose-2"'));
  assert.ok(kickoff.includes('Pursers role: worker. Capability tier_max: 1.'));
  assert.ok(kickoff.includes('board-only'));
  assert.ok(kickoff.includes('Read AGENTS.md in your seat folder'));
  const custom = buildSeatKickoff(spec, Object.assign({}, spec.seats[0], { kickoff_prompt: 'OPERATOR VERBATIM LINE' }));
  assert.ok(custom.includes('OPERATOR VERBATIM LINE'));
  const brief = buildLeadBrief(spec);
  assert.ok(brief.includes('monitor-only lead'));
  assert.ok(brief.includes('No automatic elastic scaling'));
});
