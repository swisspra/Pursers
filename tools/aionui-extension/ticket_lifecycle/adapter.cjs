'use strict';

const { spawn } = require('node:child_process');
const readline = require('node:readline');

const SAFE_ID = /^[A-Za-z0-9._-]{1,100}$/;
const PRIORITIES = new Set(['low', 'medium', 'high', 'critical']);
const SCOPES = new Set(['READ-ONLY', 'interactive-no-send', 'interactive']);
const TERMINAL = new Set(['closed', 'canceled', 'terminated']);

function failure(code, message, retryable = false) {
  return { ok: false, error: { code, message, retryable } };
}

function text(value, field, maximum, required = false) {
  if (value === undefined || value === null) {
    if (required) throw new Error(`${field} is required`);
    return undefined;
  }
  if (typeof value !== 'string') throw new Error(`${field} must be text`);
  const selected = value.trim();
  if ((required && !selected) || selected.length > maximum) {
    throw new Error(`${field} must contain ${required ? `1-${maximum}` : `at most ${maximum}`} characters`);
  }
  return selected || undefined;
}

function stringList(value, field, maximumItems = 50) {
  if (value === undefined) return undefined;
  const items = Array.isArray(value)
    ? value
    : typeof value === 'string'
      ? value.split(/[,\n]/)
      : null;
  if (!items) throw new Error(`${field} must be a list`);
  const clean = items.map((item) => text(item, field, 1000, true));
  if (clean.length < 1 || clean.length > maximumItems) throw new Error(`${field} must contain 1-${maximumItems} items`);
  return [...new Set(clean)];
}

function validateCreate(input) {
  const priority = input.priority || 'medium';
  const scope = input.scope;
  const tier = input.tier === undefined ? 2 : Number(input.tier);
  if (!PRIORITIES.has(priority)) throw new Error('priority must be low, medium, high, or critical');
  if (!SCOPES.has(scope)) throw new Error('scope must be READ-ONLY, interactive-no-send, or interactive');
  if (![1, 2, 3].includes(tier)) throw new Error('tier must be 1, 2, or 3');
  const requiredFields = stringList(input.required_fields, 'required_fields');
  if (!requiredFields) throw new Error('required_fields is required');
  const result = {
    title: text(input.title, 'title', 200, true),
    description: text(input.description, 'description', 5000, true),
    target_url: text(input.target_url, 'target_url', 500, true),
    scope,
    required_fields: requiredFields,
    forbidden: stringList(input.forbidden, 'forbidden'),
    priority,
    tier,
    tags: stringList(input.tags, 'tags'),
    related_files: stringList(input.related_files, 'related_files'),
  };
  return Object.fromEntries(Object.entries(result).filter(([, value]) => value !== undefined));
}

function createTicketLifecycleAdapter(options) {
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
      if (operation === 'create') Object.assign(payload, validateCreate(input));
      if (operation === 'get' || operation === 'cancel') {
        const ticketId = text(input.ticket_id, 'ticket_id', 100, true);
        if (!SAFE_ID.test(ticketId)) throw new Error('ticket_id contains unsupported characters');
        payload.ticket_id = ticketId;
      }
      if (operation === 'cancel') payload.reason = text(input.reason, 'reason', 2000);
      const result = await invoke(operation, payload);
      if (!result || typeof result !== 'object' || typeof result.ok !== 'boolean') {
        return failure('invalid_backend_response', 'The board helper returned an invalid response.', true);
      }
      return result;
    } catch (error) {
      if (error && error.code === 'backend_unavailable') {
        return failure('backend_unavailable', 'The board connection is unavailable. Restart or reconnect the helper, then retry.', true);
      }
      return failure('invalid_input', error instanceof Error ? error.message : 'Invalid request.');
    }
  }

  return {
    status: () => call('status'),
    list: () => call('list'),
    get: (input) => call('get', input),
    create: (input) => call('create', input),
    cancel: (input) => call('cancel', input),
    actionFor(ticket) {
      return ticket && !TERMINAL.has(ticket.status) ? 'cancel' : null;
    },
  };
}

function createTicketLifecycleProcess(options) {
  const command = options.command || 'pursers-wait-bridge';
  const args = ['ticket-lifecycle', '--state-dir', options.stateDir, '--board', options.board];
  let child;
  let lines;
  let nextId = 1;
  const pending = new Map();

  function rejectAll() {
    const error = Object.assign(new Error('ticket lifecycle backend unavailable'), { code: 'backend_unavailable' });
    for (const entry of pending.values()) entry.reject(error);
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
        reject(Object.assign(new Error('ticket lifecycle backend timed out'), { code: 'backend_unavailable' }));
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

module.exports = { createTicketLifecycleAdapter, createTicketLifecycleProcess, validateCreate };
