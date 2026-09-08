'use strict';

/**
 * Bounded AionUi Team lifecycle adapter for the Pursers extension.
 *
 * Contract: team/TEAM_ADAPTER_CONTRACT.md (TK-628602eedb90).
 * Host evidence: team/HOST_API_EVIDENCE.md — installed AionUi 2.2.1 with
 * aioncore v0.2.1, agent-facing contract "agent-facing-team-cli" schema_version 1.
 *
 * Ground rules implemented here:
 *  - only the documented agent-facing team_* surface is called;
 *  - dry-run is the default, live mutation requires options.confirm === "apply-live";
 *  - idempotent reconcile by seat name; another active identity is never stolen;
 *  - unsupported host surfaces return outcome "unsupported_by_host", never a guess;
 *  - no auto elastic scaling; Pursers role/tier_max travel in the kickoff prompt.
 */

const { execFile } = require('node:child_process');

const CONTRACT = 'agent-facing-team-cli';
const CONTRACT_SCHEMA_VERSION = 1;
const CLI_TIMEOUT_MS = 30000;

const OUTCOMES = Object.freeze({
  OK: 'ok',
  DRY_RUN: 'dry_run',
  SKIPPED_EXISTS: 'skipped_exists',
  IDENTITY_CONFLICT: 'identity_conflict',
  UNSUPPORTED_BY_HOST: 'unsupported_by_host',
  FAILED: 'failed',
});

// Exact enum published by `aioncore team capabilities` -> data.errors.
const HOST_ERROR_CODES = Object.freeze([
  'unknown_tool',
  'schema_validation_failed',
  'permission_denied',
  'team_not_found',
  'conversation_not_found',
  'agent_not_found',
  'not_in_team',
  'transport_unavailable',
  'runtime_context_missing',
  'runtime_auth_failed',
]);

const SEAT_ROLES = Object.freeze(['worker', 'reviewer']);
const TIERS = Object.freeze([1, 2, 3]);

function transportFailure(message) {
  return {
    success: false,
    error: { code: 'transport_unavailable', message },
    meta: { schema_version: CONTRACT_SCHEMA_VERSION },
  };
}

/**
 * Default CLI transport: `aioncore team <command...>` with a JSON object on
 * stdin and the stdout envelope { success, data, error, meta.schema_version }.
 * AIONUI_HELPER_BIN wins over PATH lookup (documented runtime env).
 */
function defaultRunCli(command, input) {
  const bin = process.env.AIONUI_HELPER_BIN || 'aioncore';
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (!settled) {
        settled = true;
        resolve(value);
      }
    };
    let child;
    try {
      child = execFile(
        bin,
        ['team', ...command],
        { encoding: 'utf8', timeout: CLI_TIMEOUT_MS, maxBuffer: 4 * 1024 * 1024 },
        (error, stdout) => {
          if (error) {
            finish(transportFailure(error.message));
            return;
          }
          try {
            finish(JSON.parse(stdout));
          } catch (parseError) {
            finish(transportFailure(`unparsable envelope: ${parseError.message}`));
          }
        },
      );
    } catch (spawnError) {
      finish(transportFailure(spawnError.message));
      return;
    }
    child.stdin.on('error', () => finish(transportFailure('stdin closed by host')));
    child.stdin.end(JSON.stringify(input || {}));
  });
}

function unwrap(envelope) {
  if (envelope && envelope.success === true) {
    return { ok: true, data: envelope.data || {} };
  }
  const error = (envelope && envelope.error) || {};
  return {
    ok: false,
    error: {
      code: HOST_ERROR_CODES.includes(error.code) ? error.code : 'transport_unavailable',
      raw_code: error.code || 'unknown',
      message: error.message || 'host call failed',
    },
  };
}

function isNonEmptyString(value) {
  return typeof value === 'string' && value.trim().length > 0;
}

function isSafeRelativeFolder(value) {
  if (!isNonEmptyString(value)) return false;
  if (value.startsWith('/') || value.startsWith('\\')) return false;
  const parts = value.split(/[\\/]+/).filter(Boolean);
  return parts.length > 0 && parts.every((part) => part !== '..' && part !== '.');
}

/** Validate a TeamSpec; returns { ok, errors } with JSON-pointer prefixed errors. */
function validateTeamSpec(spec) {
  const errors = [];
  if (!spec || typeof spec !== 'object') {
    return { ok: false, errors: ['/: spec must be an object'] };
  }
  if (!spec.team || !isNonEmptyString(spec.team.name)) {
    errors.push('/team/name: required non-empty string (existing Team display name)');
  }
  if (!spec.lead || !isNonEmptyString(spec.lead.name)) {
    errors.push('/lead/name: required non-empty string');
  }
  if (!spec.lead || !isNonEmptyString(spec.lead.assistant_id)) {
    errors.push('/lead/assistant_id: required non-empty string');
  }
  if (!Array.isArray(spec.seats)) {
    errors.push('/seats: required array (may be empty)');
  } else {
    const names = new Set();
    const folders = new Set();
    spec.seats.forEach((seat, index) => {
      const at = `/seats/${index}`;
      if (!seat || typeof seat !== 'object') {
        errors.push(`${at}: seat must be an object`);
        return;
      }
      if (!isNonEmptyString(seat.name)) errors.push(`${at}/name: required non-empty string`);
      if (!isNonEmptyString(seat.assistant_id)) errors.push(`${at}/assistant_id: required non-empty string`);
      if (seat.role !== undefined && !SEAT_ROLES.includes(seat.role)) {
        errors.push(`${at}/role: must be one of ${SEAT_ROLES.join(', ')}`);
      }
      if (seat.tier_max !== undefined && !TIERS.includes(seat.tier_max)) {
        errors.push(`${at}/tier_max: must be one of ${TIERS.join(', ')}`);
      }
      if (seat.model !== undefined && !isNonEmptyString(seat.model)) {
        errors.push(`${at}/model: must be a non-empty string when present`);
      }
      if (!isSafeRelativeFolder(seat.folder)) {
        errors.push(`${at}/folder: required relative path inside the Team workspace (no leading /, no .. segments)`);
      }
      if (isNonEmptyString(seat.name)) {
        if (names.has(seat.name)) errors.push(`${at}/name: duplicate seat name "${seat.name}"`);
        names.add(seat.name);
      }
      if (isSafeRelativeFolder(seat.folder)) {
        if (folders.has(seat.folder)) errors.push(`${at}/folder: duplicate seat folder "${seat.folder}"`);
        folders.add(seat.folder);
      }
    });
  }
  const options = spec.options || {};
  if (options.send_kickoff !== undefined
    && options.send_kickoff !== true
    && options.send_kickoff !== false
    && options.send_kickoff !== 'always') {
    errors.push('/options/send_kickoff: must be true, false, or "always"');
  }
  if (options.confirm !== undefined && options.confirm !== '' && options.confirm !== 'apply-live') {
    errors.push('/options/confirm: must be "" or "apply-live"');
  }
  if (Array.isArray(options.tasks)) {
    options.tasks.forEach((task, index) => {
      if (!task || !isNonEmptyString(task.subject)) {
        errors.push(`/options/tasks/${index}/subject: required non-empty string`);
      }
    });
  }
  return { ok: errors.length === 0, errors };
}

/** Prompt text carrying the Pursers invariants for one seat (role/tier/folder). */
function buildSeatKickoff(spec, seat) {
  const parts = [
    `You are the Pursers seat named ${seat.name} in AionUi Team "${spec.team.name}".`,
    `Your workspace is your seat folder "${seat.folder}" inside the shared Team workspace (one folder per seat). Work only inside that folder or inside the routed fleet clone for a claimed ticket.`,
    `Pursers role: ${seat.role || 'worker'}. Capability tier_max: ${seat.tier_max === undefined ? 2 : seat.tier_max}. These operator settings are fixed; never claim work above tier_max and never review your own work.`,
    'Task dispatch is board-only: accept work exclusively through Pursers board tickets via the seat kit bin/board.sh (wait, claim, renew, submit) in your seat folder. The AionUi team task board is for coordination visibility only.',
    isNonEmptyString(seat.kickoff_prompt)
      ? String(seat.kickoff_prompt)
      : 'Read AGENTS.md in your seat folder and follow it exactly, then start the relentless seat loop.',
  ];
  return parts.join('\n');
}

/** Prompt text for the monitor-only lead invariant. */
function buildLeadBrief(spec) {
  return [
    `You are the monitor-only lead of AionUi Team "${spec.team.name}".`,
    'Monitor the roster and relay coordinator decisions; do not perform Pursers ticket work yourself.',
    'Dispatch work only through Pursers board tickets; each teammate is an independent seat with its own folder.',
    'No automatic elastic scaling: never spawn, shut down, or rename teammates unless the operator explicitly asks.',
  ].join('\n');
}

function summarize(results) {
  const summary = {
    ok: 0, dry_run: 0, skipped_exists: 0, identity_conflict: 0,
    failed: 0, unsupported_by_host: 0,
  };
  for (const result of results) {
    if (Object.prototype.hasOwnProperty.call(summary, result.outcome)) {
      summary[result.outcome] += 1;
    }
  }
  return summary;
}

function findMemberByName(members, name) {
  return members.find((member) => member && member.name === name) || null;
}

function identityMismatch(member, seat) {
  if (member.assistant_id !== seat.assistant_id) {
    return `assistant_id differs (roster ${member.assistant_id} vs spec ${seat.assistant_id})`;
  }
  if (seat.model !== undefined && member.model && member.model !== seat.model) {
    return `model differs (roster ${member.model} vs spec ${seat.model})`;
  }
  return null;
}

function createTeamAdapter(deps = {}) {
  const runCli = typeof deps.runCli === 'function' ? deps.runCli : defaultRunCli;

  const call = async (command, input) => unwrap(await runCli(command, input || {}));

  async function roster() {
    const result = await call(['members'], {});
    if (!result.ok) return result;
    const members = Array.isArray(result.data.members) ? result.data.members : [];
    return { ok: true, members };
  }

  async function catalog() {
    const result = await call(['list-assistants'], {});
    if (!result.ok) return result;
    const assistants = Array.isArray(result.data.assistants) ? result.data.assistants : [];
    return { ok: true, assistants };
  }

  async function status(options = {}) {
    const membersResult = await roster();
    if (!membersResult.ok) {
      return { op: 'status', ok: false, error: membersResult.error, results: [], summary: summarize([]) };
    }
    const out = {
      op: 'status',
      ok: true,
      members: membersResult.members,
      counts: {
        members: membersResult.members.length,
        lead: membersResult.members.filter((m) => m.role === 'lead').length,
        teammate: membersResult.members.filter((m) => m.role === 'teammate').length,
      },
    };
    if (options.tasks === true) {
      const tasks = await call(['task', 'list'], {});
      out.tasks = tasks.ok ? (tasks.data.tasks || []) : { error: tasks.error };
    }
    return out;
  }

  /** Diff spec against the live roster + assistant catalog. Never mutates. */
  async function plan(spec) {
    const validation = validateTeamSpec(spec);
    if (!validation.ok) {
      return {
        op: 'plan', ok: false, dry_run: true,
        error: { code: 'schema_validation_failed', message: validation.errors.join('; ') },
        results: [], summary: summarize([]),
      };
    }
    const membersResult = await roster();
    if (!membersResult.ok) {
      return { op: 'plan', ok: false, dry_run: true, error: membersResult.error, results: [], summary: summarize([]) };
    }
    const catalogResult = await catalog();
    if (!catalogResult.ok) {
      return { op: 'plan', ok: false, dry_run: true, error: catalogResult.error, results: [], summary: summarize([]) };
    }
    const members = membersResult.members;
    const assistants = catalogResult.assistants;
    const results = [];

    const leadMember = members.find((member) => member && member.role === 'lead');
    if (!leadMember) {
      results.push({ seat: spec.lead.name, action: 'lead-check', outcome: OUTCOMES.FAILED, detail: 'lead_not_found: roster has no member with role "lead"' });
    } else if (leadMember.name !== spec.lead.name) {
      results.push({ seat: spec.lead.name, action: 'lead-check', outcome: OUTCOMES.FAILED, detail: `lead_name_mismatch: roster lead is "${leadMember.name}"`, slot_id: leadMember.slot_id });
    } else if (leadMember.assistant_id !== spec.lead.assistant_id) {
      results.push({ seat: spec.lead.name, action: 'lead-check', outcome: OUTCOMES.FAILED, detail: `lead_assistant_mismatch: roster ${leadMember.assistant_id} vs spec ${spec.lead.assistant_id}`, slot_id: leadMember.slot_id });
    } else {
      results.push({ seat: spec.lead.name, action: 'lead-check', outcome: OUTCOMES.OK, slot_id: leadMember.slot_id, detail: 'lead matches spec' });
    }

    const knownAssistant = (id) => assistants.some((assistant) => assistant && assistant.assistant_id === id);

    for (const seat of spec.seats) {
      if (!knownAssistant(seat.assistant_id)) {
        results.push({ seat: seat.name, action: 'spawn', outcome: OUTCOMES.FAILED, detail: `assistant_not_found: "${seat.assistant_id}" is not in team_list_assistants` });
        continue;
      }
      const member = findMemberByName(members, seat.name);
      if (!member) {
        results.push({ seat: seat.name, action: 'spawn', outcome: OUTCOMES.OK, detail: 'would spawn (name absent from roster)' });
        continue;
      }
      const mismatch = identityMismatch(member, seat);
      if (mismatch) {
        results.push({ seat: seat.name, slot_id: member.slot_id, action: 'spawn', outcome: OUTCOMES.IDENTITY_CONFLICT, detail: mismatch });
        continue;
      }
      results.push({ seat: seat.name, slot_id: member.slot_id, action: 'reuse', outcome: OUTCOMES.SKIPPED_EXISTS, detail: 'roster member matches spec identity' });
    }
    return { op: 'plan', ok: true, dry_run: true, results, summary: summarize(results) };
  }

  async function extractSlotAfterSpawn(spawnData, seatName) {
    if (spawnData && typeof spawnData === 'object') {
      if (isNonEmptyString(spawnData.slot_id)) return spawnData.slot_id;
      if (spawnData.agent && isNonEmptyString(spawnData.agent.slot_id)) return spawnData.agent.slot_id;
    }
    // Ack shape unverified live (mutation forbidden in TK-628602eedb90):
    // fall back to a roster re-read and match the seat by name.
    const refreshed = await roster();
    if (refreshed.ok) {
      const member = findMemberByName(refreshed.members, seatName);
      if (member && isNonEmptyString(member.slot_id)) return member.slot_id;
    }
    return null;
  }

  /**
   * Idempotent apply. Dry-run unless options.confirm === "apply-live" and
   * options.dry_run !== true (ticket scope "interactive-no-send").
   */
  async function apply(spec) {
    const options = Object.assign({ dry_run: true, send_kickoff: true, create_tasks: false }, spec && spec.options);
    const live = options.confirm === 'apply-live' && options.dry_run !== true;
    const planned = await plan(spec);
    if (!planned.ok) {
      return { op: 'apply', ok: false, dry_run: true, error: planned.error, results: [], summary: summarize([]) };
    }
    const results = [];
    for (const item of planned.results) {
      if (item.action === 'lead-check') {
        results.push(Object.assign({}, item, { outcome: live ? item.outcome : (item.outcome === OUTCOMES.OK ? OUTCOMES.OK : item.outcome) }));
        continue;
      }
      if (item.outcome === OUTCOMES.IDENTITY_CONFLICT || item.outcome === OUTCOMES.FAILED) {
        results.push(item);
        continue;
      }
      if (!live) {
        results.push(Object.assign({}, item, {
          outcome: OUTCOMES.DRY_RUN,
          detail: item.action === 'spawn'
            ? 'would call team_spawn_agent {name, assistant_id}' + (options.send_kickoff ? ' then team_send_message kickoff' : '')
            : 'would reuse existing slot_id',
        }));
        continue;
      }
      const seat = spec.seats.find((candidate) => candidate.name === item.seat);
      if (item.action === 'reuse') {
        results.push(Object.assign({}, item, { outcome: OUTCOMES.SKIPPED_EXISTS }));
        if (options.send_kickoff === 'always') {
          results.push(await sendKickoff(spec, seat, item.slot_id));
        }
        continue;
      }
      // action === 'spawn'
      const spawned = await call(['spawn-agent'], { name: seat.name, assistant_id: seat.assistant_id });
      if (!spawned.ok) {
        results.push({ seat: seat.name, action: 'spawn', outcome: OUTCOMES.FAILED, detail: `${spawned.error.code}: ${spawned.error.message}` });
        continue;
      }
      const slotId = await extractSlotAfterSpawn(spawned.data, seat.name);
      if (!slotId) {
        results.push({ seat: seat.name, action: 'spawn', outcome: OUTCOMES.FAILED, detail: 'spawn_ack_unparsed: host acknowledged spawn but no slot_id could be resolved' });
        continue;
      }
      results.push({ seat: seat.name, slot_id: slotId, action: 'spawn', outcome: OUTCOMES.OK, detail: 'team_spawn_agent acknowledged' });
      if (options.send_kickoff === true || options.send_kickoff === 'always') {
        results.push(await sendKickoff(spec, seat, slotId));
      }
    }
    if (live && options.create_tasks === true && Array.isArray(options.tasks)) {
      for (const task of options.tasks) {
        const payload = { subject: task.subject };
        if (isNonEmptyString(task.description)) payload.description = task.description;
        if (isNonEmptyString(task.owner_seat)) {
          const ownerSlot = results.find((entry) => entry.seat === task.owner_seat && isNonEmptyString(entry.slot_id));
          if (ownerSlot) payload.owner = ownerSlot.slot_id;
        }
        if (Array.isArray(task.blocked_by) && task.blocked_by.length > 0) payload.blocked_by = task.blocked_by;
        const created = await call(['task', 'create'], payload);
        results.push({
          seat: task.owner_seat || '(unassigned)',
          action: 'task',
          outcome: created.ok ? OUTCOMES.OK : OUTCOMES.FAILED,
          detail: created.ok ? 'team_task_create acknowledged' : `${created.error.code}: ${created.error.message}`,
        });
      }
    }
    return { op: 'apply', ok: true, dry_run: !live, results, summary: summarize(results) };
  }

  async function sendKickoff(spec, seat, slotId) {
    const message = buildSeatKickoff(spec, seat);
    const sent = await call(['send-message'], { to: slotId, message });
    return {
      seat: seat.name,
      slot_id: slotId,
      action: 'kickoff',
      outcome: sent.ok ? OUTCOMES.OK : OUTCOMES.FAILED,
      detail: sent.ok ? 'kickoff delivered via team_send_message' : `${sent.error.code}: ${sent.error.message}`,
    };
  }

  async function pauseSeat(slotId, message, reason) {
    if (!isNonEmptyString(slotId) || !isNonEmptyString(message)) {
      return single('pause', slotId, 'interrupt', OUTCOMES.FAILED, 'schema_validation_failed: slot_id and message are required');
    }
    const payload = { slot_id: slotId, message };
    if (reason !== undefined) payload.reason = reason;
    const result = await call(['interrupt-agent'], payload);
    return single('pause', slotId, 'interrupt', result.ok ? OUTCOMES.OK : OUTCOMES.FAILED,
      result.ok ? 'turn interrupted and replacement instruction delivered' : `${result.error.code}: ${result.error.message}`);
  }

  async function stopSeat(slotId, reason) {
    if (!isNonEmptyString(slotId)) {
      return single('stop', slotId, 'shutdown', OUTCOMES.FAILED, 'schema_validation_failed: slot_id is required');
    }
    const payload = { slot_id: slotId };
    if (reason !== undefined) payload.reason = reason;
    const result = await call(['shutdown-agent'], payload);
    return single('stop', slotId, 'shutdown', result.ok ? OUTCOMES.OK : OUTCOMES.FAILED,
      result.ok ? 'shutdown requested; cooperative — the teammate may answer shutdown_rejected: <reason>' : `${result.error.code}: ${result.error.message}`);
  }

  async function renameSeat(slotId, newName) {
    if (!isNonEmptyString(slotId) || !isNonEmptyString(newName)) {
      return single('rename', slotId, 'rename', OUTCOMES.FAILED, 'schema_validation_failed: slot_id and new_name are required');
    }
    const result = await call(['rename-agent'], { slot_id: slotId, new_name: newName });
    return single('rename', slotId, 'rename', result.ok ? OUTCOMES.OK : OUTCOMES.FAILED,
      result.ok ? 'renamed' : `${result.error.code}: ${result.error.message}`);
  }

  async function resetSeatContext(slotId) {
    if (!isNonEmptyString(slotId)) {
      return single('reset-context', slotId, 'clear-context', OUTCOMES.FAILED, 'schema_validation_failed: slot_id is required');
    }
    const result = await call(['clear-agent-context'], { slot_id: slotId });
    return single('reset-context', slotId, 'clear-context', result.ok ? OUTCOMES.OK : OUTCOMES.FAILED,
      result.ok ? 'context cleared' : `${result.error.code}: ${result.error.message}`);
  }

  function single(op, slotId, action, outcome, detail) {
    const results = [{ slot_id: slotId || null, action, outcome, detail }];
    return { op, ok: outcome !== OUTCOMES.FAILED, dry_run: false, results, summary: summarize(results) };
  }

  function removeSeat(slotId) {
    const results = [{
      slot_id: slotId || null,
      action: 'remove',
      outcome: OUTCOMES.UNSUPPORTED_BY_HOST,
      detail: 'contract agent-facing-team-cli schema_version 1 exposes no teammate-removal tool; observed-only (unverified) REST route /api/teams/{id}/agents/{slot_id}',
    }];
    return { op: 'remove', ok: true, dry_run: false, results, summary: summarize(results) };
  }

  function createTeam() {
    const results = [{
      seat: null, action: 'create-team', outcome: OUTCOMES.UNSUPPORTED_BY_HOST,
      detail: 'contract agent-facing-team-cli schema_version 1 exposes no team-create tool; Teams are created via the AionUi GUI/REST layer (observed-only route /api/teams, unverified)',
    }];
    return { op: 'create-team', ok: true, dry_run: false, results, summary: summarize(results) };
  }

  function archiveTeam() {
    const results = [{
      seat: null, action: 'archive-team', outcome: OUTCOMES.UNSUPPORTED_BY_HOST,
      detail: 'no agent-facing archive tool; persistence column teams.archived_at observed read-only; GUI/REST path unverified',
    }];
    return { op: 'archive-team', ok: true, dry_run: false, results, summary: summarize(results) };
  }

  return Object.freeze({
    contract: CONTRACT,
    schemaVersion: CONTRACT_SCHEMA_VERSION,
    status,
    plan,
    apply,
    pauseSeat,
    stopSeat,
    renameSeat,
    resetSeatContext,
    removeSeat,
    createTeam,
    archiveTeam,
    listAssistants: catalog,
    describeAssistant: (assistantId, locale) => call(
      ['describe-assistant'],
      locale === undefined ? { assistant_id: assistantId } : { assistant_id: assistantId, locale },
    ),
    buildSeatKickoff: (spec, seat) => buildSeatKickoff(spec, seat),
    buildLeadBrief: (spec) => buildLeadBrief(spec),
  });
}

module.exports = {
  CONTRACT,
  CONTRACT_SCHEMA_VERSION,
  OUTCOMES,
  HOST_ERROR_CODES,
  SEAT_ROLES,
  TIERS,
  createTeamAdapter,
  validateTeamSpec,
  buildSeatKickoff,
  buildLeadBrief,
  defaultRunCli,
};
