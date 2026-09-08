'use strict';

const DOOR_PREFIX = 'prs1.';
const SAFE_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/;
const SAFE_BOARD = /^[A-Za-z0-9._-]{1,80}$/;
const ROLES = new Set(['worker', 'reviewer']);

function result(operation, outcome, fields = {}) {
  return { ok: outcome === 'ready' || outcome === 'connected' || outcome === 'recovered' || outcome === 'rotated', operation, outcome, ...fields };
}

function failure(operation, code, message, fields = {}) {
  return result(operation, 'failed', { code, message, retryable: false, ...fields });
}

function decodeSegment(value, label) {
  if (typeof value !== 'string' || !value || !/^[A-Za-z0-9_-]+$/.test(value)) {
    throw new Error(`${label} is not base64url`);
  }
  let decoded;
  try {
    decoded = JSON.parse(Buffer.from(value, 'base64url').toString('utf8'));
  } catch (_error) {
    throw new Error(`${label} is not valid JSON`);
  }
  if (!decoded || typeof decoded !== 'object' || Array.isArray(decoded)) {
    throw new Error(`${label} must be a JSON object`);
  }
  return decoded;
}

function loopbackHostname(hostname) {
  const normalized = String(hostname || '').replace(/^\[|\]$/g, '').replace(/\.$/, '').toLowerCase();
  return normalized === 'localhost' || normalized.endsWith('.localhost') || normalized === '::1' || normalized.startsWith('127.');
}

function parseDoor(door, nowEpoch = Math.floor(Date.now() / 1000)) {
  const operation = 'parse';
  if (typeof door !== 'string' || door.length > 16384 || !door.startsWith(DOOR_PREFIX)) {
    return failure(operation, 'invalid_door', 'Ask your coordinator for a valid Pursers door.');
  }
  try {
    const envelope = decodeSegment(door.slice(DOOR_PREFIX.length), 'door payload');
    if (Object.keys(envelope).sort().join(',') !== 'b,r,t,u') {
      return failure(operation, 'invalid_door', 'The door payload has an unsupported shape. Ask your coordinator for a new door.');
    }
    if (![envelope.u, envelope.b, envelope.r, envelope.t].every((value) => typeof value === 'string' && value.trim())) {
      return failure(operation, 'invalid_door', 'The door payload has empty fields. Ask your coordinator for a new door.');
    }
    if (!SAFE_BOARD.test(envelope.b)) {
      return failure(operation, 'invalid_board', 'The door contains an invalid board identifier.');
    }
    if (!ROLES.has(envelope.r)) {
      return failure(operation, 'invalid_role', 'The door role must be worker or reviewer.');
    }
    let endpoint;
    try {
      endpoint = new URL(envelope.u);
    } catch (_error) {
      return failure(operation, 'invalid_url', 'The door contains an invalid Central URL.');
    }
    if (endpoint.username || endpoint.password || endpoint.hash) {
      return failure(operation, 'invalid_url', 'The Central URL must not contain credentials or a fragment.');
    }
    const loopback = loopbackHostname(endpoint.hostname);
    if (endpoint.protocol !== 'https:' && !(endpoint.protocol === 'http:' && loopback)) {
      return failure(operation, 'insecure_remote_url', 'Remote Central requires HTTPS; HTTP is accepted only on loopback.');
    }
    const tokenParts = envelope.t.split('.');
    if (tokenParts.length !== 3) {
      return failure(operation, 'invalid_door', 'The door credential is malformed. Ask your coordinator for a new door.');
    }
    const header = decodeSegment(tokenParts[0], 'credential header');
    const claims = decodeSegment(tokenParts[1], 'credential claims');
    if (typeof header.kid !== 'string' || !header.kid || !Number.isInteger(claims.exp)) {
      return failure(operation, 'invalid_door', 'The door credential is missing key or expiry metadata.');
    }
    if (claims.exp <= nowEpoch) {
      return failure(operation, 'expired_door', 'This door has expired. Ask your coordinator for a replacement door.');
    }
    return result(operation, 'ready', {
      metadata: {
        board: envelope.b,
        role: envelope.r,
        kid: header.kid,
        exp: claims.exp,
        transport: endpoint.protocol === 'https:' ? 'https' : 'http-loopback',
        remote: !loopback,
      },
      private: { door, centralUrl: envelope.u },
    });
  } catch (_error) {
    return failure(operation, 'invalid_door', 'The door cannot be decoded. Ask your coordinator for a new door.');
  }
}

function validateInput(input, nowEpoch) {
  const parsed = parseDoor(input && input.door, nowEpoch);
  if (!parsed.ok) return { ...parsed, operation: 'validate' };
  const { metadata } = parsed;
  if (input.expected_board && input.expected_board !== metadata.board) {
    return failure('validate', 'wrong_board', `Use a door issued for board ${input.expected_board}.`);
  }
  if (input.expected_role && input.expected_role !== metadata.role) {
    return failure('validate', 'wrong_role', `Use a ${input.expected_role} door for this seat.`);
  }
  const seatName = input.seat_name == null ? null : String(input.seat_name).trim();
  if (seatName !== null && !SAFE_NAME.test(seatName)) {
    return failure('validate', 'invalid_seat_name', 'Seat name must be a safe 1-80 character identifier.');
  }
  const tierMax = input.tier_max == null ? 2 : input.tier_max;
  if (![1, 2, 3].includes(tierMax)) {
    return failure('validate', 'invalid_tier', 'tier_max must be 1, 2, or 3.');
  }
  for (const field of ['assistant_id', 'model']) {
    if (input[field] != null && (typeof input[field] !== 'string' || !input[field].trim())) {
      return failure('validate', `invalid_${field}`, `${field} must be a non-empty string when supplied.`);
    }
  }
  if (input.folder != null && (typeof input.folder !== 'string' || !SAFE_NAME.test(input.folder.trim()))) {
    return failure('validate', 'invalid_folder', 'folder must be a safe relative seat directory name.');
  }
  return result('validate', 'ready', {
    metadata,
    normalized: {
      seat_name: seatName,
      tier_max: tierMax,
      assistant_id: input.assistant_id && input.assistant_id.trim(),
      model: input.model && input.model.trim(),
      folder: input.folder && input.folder.trim(),
    },
    private: parsed.private,
  });
}

function keyValues(line) {
  const values = {};
  for (const field of String(line).trim().split(/\s+/)) {
    const separator = field.indexOf('=');
    if (separator > 0) values[field.slice(0, separator)] = field.slice(separator + 1);
  }
  return values;
}

function parseJoinOutput(output) {
  return Object.assign({}, ...String(output).split(/\r?\n/).filter(Boolean).map(keyValues));
}

function parseStatusOutput(output) {
  const lines = String(output).split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  let pushMode = 'unknown';
  const seats = [];
  for (const line of lines) {
    const values = keyValues(line);
    if (values.push_mode) pushMode = values.push_mode;
    if (values.board && values.role) {
      seats.push({
        board: values.board,
        role: values.role,
        kid: values.kid || 'unknown',
        exp: Number(values.exp) || 0,
        seat_names: values.seat_names_used === '-' ? [] : (values.seat_names_used || '').split(',').filter(Boolean),
      });
    }
  }
  return { push_mode: pushMode, seats };
}

function bridgeFailure(operation, error) {
  if (error && (error.code === 'ENOENT' || error.errno === -2)) {
    return failure(operation, 'bridge_not_installed', 'Install pursers-wait-bridge, then retry.', { retryable: true });
  }
  if (operation === 'status') {
    return failure(operation, 'bridge_status_failed', 'Stored door status is unavailable. Check the bridge state and retry.', { retryable: true });
  }
  const detail = String((error && error.stderr) || '');
  if (/expired/i.test(detail)) return failure(operation, 'expired_door', 'This door has expired. Ask your coordinator for a replacement door.');
  if (/already active|collision/i.test(detail)) return failure(operation, 'identity_conflict', 'This seat name is already active. Choose a unique seat name.', { retryable: true });
  if (/--rotate requires/i.test(detail)) return failure(operation, 'rotation_requires_existing', 'Connect the original board and role before rotating.', { retryable: true });
  if (/already exists.*--rotate/i.test(detail)) return failure(operation, 'rotation_required', 'Use the rotate operation for a replacement door.', { retryable: true });
  if (/ECONNREFUSED|timed? out|unreachable|transport|connection/i.test(detail)) {
    return failure(operation, 'server_unreachable', 'Central is unreachable. Check connectivity and retry.', { retryable: true });
  }
  return failure(operation, 'bridge_rejected', 'The bridge rejected the door. Check the board, role, and door validity.', { retryable: true });
}

function teamSeat(normalized, joined, metadata) {
  const seat = {
    name: joined.seat_name,
    role: metadata.role,
    tier_max: normalized.tier_max,
    folder: normalized.folder || joined.seat_name,
  };
  if (normalized.assistant_id) seat.assistant_id = normalized.assistant_id;
  if (normalized.model) seat.model = normalized.model;
  return {
    outcome: seat.assistant_id ? 'ready' : 'needs_assistant',
    detail: seat.assistant_id ? 'Seat fragment is ready for TeamSpec.seats[].' : 'Select an assistant_id before Team planning.',
    seat,
  };
}

function createDoorOnboarding(dependencies = {}) {
  if (typeof dependencies.runBridge !== 'function') throw new TypeError('runBridge dependency is required');
  if (typeof dependencies.importMcp !== 'function') throw new TypeError('importMcp dependency is required');
  const nowEpoch = dependencies.nowEpoch || (() => Math.floor(Date.now() / 1000));

  async function status() {
    try {
      const parsed = parseStatusOutput(await dependencies.runBridge(['status'], {}));
      return result('status', 'ready', parsed);
    } catch (error) {
      return bridgeFailure('status', error);
    }
  }

  async function register(operation, joined, metadata, normalized, context, outcome) {
    const serverName = `Pursers ${metadata.role} ${metadata.board}`;
    try {
      const imported = await dependencies.importMcp({
        name: serverName,
        transport: { type: 'stdio', command: 'pursers-wait-bridge', args: [], env: {} },
      }, context);
      if (!imported || imported.success === false) throw new Error('import rejected');
    } catch (_error) {
      return failure(operation, 'mcp_registration_failed', 'The seat connected, but MCP registration failed. Use recover and retry.', {
        outcome: 'partial',
        retryable: true,
        connected: true,
        status: joined,
        team: teamSeat(normalized, joined, metadata),
      });
    }
    return result(operation, outcome, {
      status: joined,
      mcp_server: serverName,
      imported: true,
      team: teamSeat(normalized, joined, metadata),
    });
  }

  async function connect(input = {}, context = {}, mode = 'connect') {
    const validated = validateInput(input, nowEpoch());
    if (!validated.ok) return { ...validated, operation: mode };
    const { metadata, normalized } = validated;
    const current = await status();
    if (!current.ok) return { ...current, operation: mode };
    const existing = current.ok && current.seats.find((seat) => seat.board === metadata.board && seat.role === metadata.role);
    if (mode === 'rotate' && !existing) {
      return failure(mode, 'rotation_requires_existing', 'Connect the original board and role before rotating.', { retryable: true });
    }
    if (existing && existing.kid !== metadata.kid && mode !== 'rotate') {
      return failure(mode, 'rotation_required', 'A different door is already stored. Use the rotate operation.', { retryable: true });
    }
    let seatName = normalized.seat_name;
    if (existing && existing.kid === metadata.kid) {
      if (!seatName && existing.seat_names.length === 1) [seatName] = existing.seat_names;
      if (!seatName && existing.seat_names.length > 1) {
        return failure(mode, 'seat_name_required', 'Choose the existing seat name to resume this connection.', { retryable: true });
      }
      if (seatName && existing.seat_names.includes(seatName)) {
        const joined = { board: metadata.board, role: metadata.role, seat_name: seatName, push_mode: current.push_mode, kid: metadata.kid, exp: metadata.exp };
        return register(mode, joined, metadata, { ...normalized, seat_name: seatName }, context, mode === 'rotate' ? 'rotated' : 'recovered');
      }
    }
    const args = ['join'];
    if (mode === 'rotate') args.push('--rotate');
    if (metadata.remote) args.push('--allow-remote');
    if (seatName) args.push('--name', seatName);
    args.push(validated.private.door);
    let joinedRaw;
    try {
      joinedRaw = parseJoinOutput(await dependencies.runBridge(args, { env: { PURSERS_TIER_MAX: String(normalized.tier_max) } }));
    } catch (error) {
      return bridgeFailure(mode, error);
    }
    if (joinedRaw.board !== metadata.board || joinedRaw.role !== metadata.role || !SAFE_NAME.test(joinedRaw.seat_name || '')) {
      return failure(mode, 'invalid_bridge_response', 'The bridge returned inconsistent seat metadata.', { retryable: true });
    }
    const after = await status();
    const stored = after.ok && after.seats.find((seat) => seat.board === metadata.board && seat.role === metadata.role);
    const joined = {
      board: metadata.board,
      role: metadata.role,
      seat_name: joinedRaw.seat_name,
      push_mode: after.ok ? after.push_mode : (joinedRaw.push === 'yes' ? 'push' : 'unknown'),
      kid: stored ? stored.kid : metadata.kid,
      exp: stored ? stored.exp : metadata.exp,
    };
    return register(mode, joined, metadata, { ...normalized, seat_name: joined.seat_name }, context, mode === 'rotate' ? 'rotated' : 'connected');
  }

  async function recover(input = {}, context = {}) {
    const operation = 'recover';
    if (!SAFE_BOARD.test(input.board || '') || !ROLES.has(input.role) || !SAFE_NAME.test(input.seat_name || '')) {
      return failure(operation, 'invalid_recovery_target', 'Recovery requires a valid board, role, and existing seat name.');
    }
    const tierMax = input.tier_max == null ? 2 : input.tier_max;
    if (![1, 2, 3].includes(tierMax)) return failure(operation, 'invalid_tier', 'tier_max must be 1, 2, or 3.');
    for (const field of ['assistant_id', 'model']) {
      if (input[field] != null && (typeof input[field] !== 'string' || !input[field].trim())) {
        return failure(operation, `invalid_${field}`, `${field} must be a non-empty string when supplied.`);
      }
    }
    if (input.folder != null && (typeof input.folder !== 'string' || !SAFE_NAME.test(input.folder.trim()))) {
      return failure(operation, 'invalid_folder', 'folder must be a safe relative seat directory name.');
    }
    const current = await status();
    if (!current.ok) return { ...current, operation };
    const existing = current.seats.find((seat) => seat.board === input.board && seat.role === input.role);
    if (!existing) return failure(operation, 'stored_door_not_found', 'No stored door matches this board and role. Connect first.');
    if (!existing.seat_names.includes(input.seat_name)) return failure(operation, 'seat_not_found', 'The requested seat is not recorded for this door.');
    const normalized = {
      seat_name: input.seat_name,
      tier_max: tierMax,
      assistant_id: input.assistant_id && String(input.assistant_id).trim(),
      model: input.model && String(input.model).trim(),
      folder: input.folder && String(input.folder).trim(),
    };
    const joined = { board: input.board, role: input.role, seat_name: input.seat_name, push_mode: current.push_mode, kid: existing.kid, exp: existing.exp };
    return register(operation, joined, existing, normalized, context, 'recovered');
  }

  return {
    parse: (door) => {
      const parsed = parseDoor(door, nowEpoch());
      if (!parsed.ok) return parsed;
      const { private: _private, ...publicResult } = parsed;
      return publicResult;
    },
    validate: (input) => {
      const validated = validateInput(input || {}, nowEpoch());
      if (!validated.ok) return validated;
      const { private: _private, ...publicResult } = validated;
      return publicResult;
    },
    connect: (input, context) => connect(input, context, 'connect'),
    rotate: (input, context) => connect(input, context, 'rotate'),
    recover,
    status,
  };
}

module.exports = {
  createDoorOnboarding,
  parseJoinOutput,
  parseStatusOutput,
};
