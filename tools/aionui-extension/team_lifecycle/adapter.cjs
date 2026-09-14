'use strict';

const { spawn } = require('node:child_process');
const readline = require('node:readline');

const SAFE_ID = /^[A-Za-z0-9._-]{1,100}$/;

function failure(code, message, retryable = false) {
  return { ok: false, error: { code, message, retryable } };
}

function validateName(value) {
  if (typeof value !== 'string' || !value.trim() || value.trim().length > 80) {
    throw new Error('name must contain 1-80 characters');
  }
  return value.trim();
}

function validateMembers(value) {
  if (!Array.isArray(value) || value.length < 1 || value.length > 50) {
    throw new Error('member_agent_ids must contain 1-50 agent IDs');
  }
  const members = value.map((item) => {
    if (typeof item !== 'string' || !SAFE_ID.test(item)) throw new Error('member_agent_ids contains an invalid agent ID');
    return item;
  });
  if (new Set(members).size !== members.length) throw new Error('member_agent_ids must be unique');
  return members;
}

function validateRevision(value) {
  if (!Number.isInteger(value) || value < 0) throw new Error('expected_revision must be a non-negative integer');
  return value;
}

function createTeamLifecycleAdapter(options) {
  const expectedBoard = String(options.expectedBoard || '');
  const invoke = options.run;
  if (!SAFE_ID.test(expectedBoard)) throw new Error('expectedBoard must be a safe board identifier');
  if (typeof invoke !== 'function') throw new Error('run must be a function');

  async function call(operation, input = {}) {
    if (input.board !== undefined && input.board !== expectedBoard) {
      return failure('board_mismatch', `Use the configured board ${expectedBoard}.`);
    }
    try {
      const payload = { board: expectedBoard };
      if (['create', 'update', 'remove'].includes(operation)) {
        payload.expected_revision = validateRevision(input.expected_revision);
      }
      if (['update', 'remove'].includes(operation)) {
        if (typeof input.group_id !== 'string' || !/^group-[0-9a-f]{12}$/.test(input.group_id)) {
          throw new Error('group_id is invalid');
        }
        payload.group_id = input.group_id;
      }
      if (['create', 'update'].includes(operation)) {
        payload.name = validateName(input.name);
        payload.member_agent_ids = validateMembers(input.member_agent_ids);
      }
      const result = await invoke(operation, payload);
      if (!result || typeof result !== 'object' || typeof result.ok !== 'boolean') {
        return failure('backend_unavailable', 'The board helper returned an invalid response.', true);
      }
      return result;
    } catch (error) {
      if (error && error.code === 'backend_unavailable') {
        return failure('backend_unavailable', 'The board connection is unavailable. Reconnect the helper, then retry.', true);
      }
      return failure('invalid_input', error instanceof Error ? error.message : 'Invalid request.');
    }
  }

  return {
    status: () => call('status'),
    list: () => call('list'),
    create: (input) => call('create', input),
    update: (input) => call('update', input),
    remove: (input) => call('remove', input),
  };
}

function createTeamLifecycleProcess(options) {
  const command = options.command || 'pursers-wait-bridge';
  const args = ['team-lifecycle', '--state-dir', options.stateDir, '--board', options.board];
  let child;
  let lines;
  let nextId = 1;
  const pending = new Map();

  function rejectAll() {
    const error = Object.assign(new Error('team lifecycle backend unavailable'), { code: 'backend_unavailable' });
    for (const entry of pending.values()) {
      clearTimeout(entry.timer);
      entry.reject(error);
    }
    pending.clear();
  }

  function ensureChild() {
    if (child && !child.killed) return;
    const env = { ...process.env };
    for (const name of [
      'AIONUI_BASE_URL', 'AIONUI_USER_ID', 'AIONUI_CONVERSATION_ID', 'AIONUI_RUNTIME_TOKEN',
      'ONBOARD_CENTRAL_TOKEN', 'ONBOARD_CENTRAL_TOKEN_FILE', 'ONBOARD_CENTRAL_URL',
      'ONBOARD_BOARD_ID', 'ONBOARD_AGENT_NAME',
    ]) delete env[name];
    child = spawn(command, args, { stdio: ['pipe', 'pipe', 'ignore'], env });
    lines = readline.createInterface({ input: child.stdout });
    lines.on('line', (line) => {
      let value;
      try { value = JSON.parse(line); } catch (_error) { return; }
      const entry = pending.get(value.id);
      if (!entry) return;
      pending.delete(value.id);
      clearTimeout(entry.timer);
      entry.resolve(value.result);
    });
    child.once('error', rejectAll);
    child.once('exit', () => { rejectAll(); child = undefined; });
  }

  function run(operation, payload) {
    ensureChild();
    const id = nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(Object.assign(new Error('team lifecycle backend timed out'), { code: 'backend_unavailable' }));
      }, options.timeoutMs || 15000);
      pending.set(id, { resolve, reject, timer });
      child.stdin.write(`${JSON.stringify({ id, operation, payload })}\n`, (error) => {
        if (!error) return;
        clearTimeout(timer);
        pending.delete(id);
        reject(Object.assign(error, { code: 'backend_unavailable' }));
      });
    });
  }

  async function close() {
    if (!child) return;
    const selected = child;
    child = undefined;
    selected.stdin.end();
    await new Promise((resolve) => {
      const timer = setTimeout(() => { selected.kill('SIGTERM'); resolve(); }, 2000);
      selected.once('exit', () => { clearTimeout(timer); resolve(); });
    });
  }

  return { run, close };
}

module.exports = { createTeamLifecycleAdapter, createTeamLifecycleProcess };
