'use strict';

const SAFE_BOARD = /^[A-Za-z0-9._-]{1,80}$/;
const SAFE_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/;
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/;
const ROLES = new Set(['worker', 'reviewer']);
const LIFECYCLES = new Set(['active', 'handed_off', 'retired', 'stale']);

function result(operation, outcome, fields = {}) {
  return {
    ok: ['joined', 'active', 'retired', 'disconnected'].includes(outcome),
    operation,
    outcome,
    ...fields,
  };
}

function failure(operation, code, message, fields = {}) {
  return result(operation, 'failed', {
    code,
    message,
    retryable: false,
    recovery: 'Check the selected seat and retry.',
    ...fields,
  });
}

function safeIdentity(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const identity = {
    board: value.board || value.board_id,
    agent_id: value.agent_id,
    principal_id: value.principal_id,
    agent_name: value.agent_name,
    role: value.role,
    lifecycle_status: value.lifecycle_status,
  };
  if (!SAFE_BOARD.test(identity.board || '')) return null;
  if (!SAFE_ID.test(identity.agent_id || '') || !SAFE_ID.test(identity.principal_id || '')) return null;
  if (!SAFE_NAME.test(identity.agent_name || '') || !ROLES.has(identity.role)) return null;
  if (!LIFECYCLES.has(identity.lifecycle_status)) return null;
  return identity;
}

function sameIdentity(left, right) {
  return ['board', 'agent_id', 'principal_id', 'agent_name', 'role']
    .every((field) => left[field] === right[field]);
}

function confirmation(identity) {
  return `retire ${identity.agent_name} from ${identity.board}`;
}

function validateSelector(input, expectedBoard) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) {
    return failure('status', 'invalid_input', 'Seat status requires a JSON object.');
  }
  if (input.board !== undefined && input.board !== expectedBoard) {
    return failure('status', 'wrong_board', `Use the configured board ${expectedBoard}.`);
  }
  if (input.agent_name !== undefined && !SAFE_NAME.test(input.agent_name)) {
    return failure('status', 'invalid_agent_name', 'Seat name must be a safe 1-80 character identifier.');
  }
  if (input.role !== undefined && !ROLES.has(input.role)) {
    return failure('status', 'invalid_role', 'Seat role must be worker or reviewer.');
  }
  return null;
}

function boardAgents(value, expectedBoard) {
  if (!value || value.ok !== true || value.board_id !== expectedBoard || !Array.isArray(value.agents)) return null;
  const agents = [];
  for (const row of value.agents) {
    const identity = safeIdentity({ ...row, board: expectedBoard });
    if (!identity) return null;
    agents.push({ identity, row });
  }
  return agents;
}

function createSeatLifecycle(dependencies = {}) {
  const expectedBoard = dependencies.expectedBoard;
  if (!SAFE_BOARD.test(expectedBoard || '')) throw new TypeError('expectedBoard must be a safe board identifier');
  for (const name of ['joinSeat', 'readBoard', 'retireSelf', 'forgetDoor']) {
    if (typeof dependencies[name] !== 'function') throw new TypeError(`${name} dependency is required`);
  }
  let boundIdentity = dependencies.initialIdentity ? safeIdentity(dependencies.initialIdentity) : null;
  if (dependencies.initialIdentity && (!boundIdentity || boundIdentity.board !== expectedBoard)) {
    throw new TypeError('initialIdentity is invalid or belongs to another board');
  }

  async function readStatus(input = {}, operation = 'status') {
    const invalid = validateSelector(input, expectedBoard);
    if (invalid) return { ...invalid, operation };
    let snapshot;
    try {
      snapshot = await dependencies.readBoard({ board: expectedBoard, include_retired: true });
    } catch (_error) {
      return failure(operation, 'board_unavailable', 'Live board state is unavailable.', {
        retryable: true,
        recovery: 'Check the helper connection, then refresh seat status.',
      });
    }
    const agents = boardAgents(snapshot, expectedBoard);
    if (!agents) {
      return failure(operation, 'invalid_board_response', 'The board returned inconsistent seat data.', {
        retryable: true,
        recovery: 'Reconnect the helper before retrying.',
      });
    }
    let matches;
    if (boundIdentity) {
      matches = agents.filter(({ identity }) => identity.agent_id === boundIdentity.agent_id);
      if (matches.length === 1 && !sameIdentity(matches[0].identity, boundIdentity)) {
        return failure(operation, 'identity_mismatch', 'The preserved seat identity no longer matches Central.', {
          recovery: 'Do not retire this seat. Reconnect with the original identity or ask the operator to inspect Central.',
        });
      }
    } else {
      if (!input.agent_name || !input.role) {
        return failure(operation, 'identity_required', 'Choose the existing seat name and role.', {
          recovery: 'Refresh stored connection status, then choose the exact seat.',
        });
      }
      matches = agents.filter(({ identity }) => identity.agent_name === input.agent_name && identity.role === input.role);
    }
    if (matches.length === 0) {
      return failure(operation, 'identity_not_found', 'The preserved seat is not present in live board state.', {
        retryable: true,
        recovery: 'Rejoin with the same seat name to reactivate it, or ask the operator to inspect membership.',
      });
    }
    if (matches.length !== 1) {
      return failure(operation, 'identity_conflict', 'Live board state contains more than one matching seat.', {
        recovery: 'Stop and ask the operator to resolve the duplicate identity.',
      });
    }
    const selected = matches[0];
    if (!boundIdentity) boundIdentity = selected.identity;
    return result(operation, selected.identity.lifecycle_status === 'retired' ? 'retired' : 'active', {
      identity: selected.identity,
      dispatch_status: selected.row.status || 'unknown',
      lease_expires_at: selected.row.lease_expires_at || null,
      current_offer: selected.row.current_offer || null,
      confirmation: confirmation(selected.identity),
    });
  }

  async function join(input = {}) {
    const invalid = validateSelector(input, expectedBoard);
    if (invalid) return { ...invalid, operation: 'join' };
    if (!SAFE_NAME.test(input.agent_name || '') || !ROLES.has(input.role)) {
      return failure('join', 'identity_required', 'Join requires the exact seat name and role.');
    }
    let joined;
    try {
      joined = await dependencies.joinSeat({ ...input, expected_board: expectedBoard });
    } catch (_error) {
      return failure('join', 'join_failed', 'Central did not accept the seat join.', {
        retryable: true,
        recovery: 'Check the door and helper connection, then retry with the same seat name.',
      });
    }
    const identity = safeIdentity(joined && (joined.identity || joined.agent || joined));
    if (!identity || identity.board !== expectedBoard || identity.agent_name !== input.agent_name || identity.role !== input.role) {
      return failure('join', 'invalid_join_response', 'The join returned inconsistent seat identity.', {
        recovery: 'Do not continue. Ask the operator to inspect the board membership.',
      });
    }
    if (identity.lifecycle_status !== 'active') {
      return failure('join', 'join_not_active', 'The joined seat is not active.', { retryable: true });
    }
    const previous = boundIdentity;
    if (previous && !sameIdentity(previous, identity)) {
      return failure('join', 'identity_mismatch', 'The join would replace the preserved seat identity.', {
        recovery: 'Use the original door and seat name; never take over a different identity.',
      });
    }
    boundIdentity = identity;
    const verified = await readStatus({}, 'join');
    if (!verified.ok || verified.identity.lifecycle_status !== 'active') {
      boundIdentity = previous;
      return failure('join', 'join_verification_failed', 'The joined identity was not verified in live board state.', {
        retryable: true,
        recovery: 'Refresh live status before retrying; do not create a different seat name.',
      });
    }
    return result('join', 'joined', {
      identity: verified.identity,
      dispatch_status: verified.dispatch_status,
      rejoined: Boolean(joined.rejoined),
      confirmation: confirmation(verified.identity),
    });
  }

  async function status(input = {}) {
    return readStatus(input, 'status');
  }

  async function disconnect(input = {}) {
    const current = await readStatus(input, 'disconnect');
    if (!current.ok) return current;
    if (input.confirm !== current.confirmation) {
      return failure('disconnect', 'confirmation_required', `Type exactly: ${current.confirmation}`, {
        recovery: 'Review the exact board and seat name before confirming retirement.',
      });
    }
    if (current.lease_expires_at) {
      return failure('disconnect', 'active_lease', 'This seat has an active work or review lease.', {
        retryable: true,
        recovery: 'Finish, submit, or release the active assignment before retiring the seat.',
      });
    }
    const identity = current.identity;
    if (identity.lifecycle_status !== 'retired') {
      let retired;
      try {
        retired = await dependencies.retireSelf({
          board: expectedBoard,
          agent_name: identity.agent_name,
        });
      } catch (_error) {
        return failure('disconnect', 'retirement_failed', 'Central refused self-retirement.', {
          retryable: true,
          recovery: 'Refresh status. Active claims must be completed or released before retrying.',
        });
      }
      const retiredIdentity = safeIdentity(retired && retired.agent);
      if (!retired || retired.ok !== true || retired.board_id !== expectedBoard || !retiredIdentity || !sameIdentity(retiredIdentity, identity)) {
        return failure('disconnect', 'invalid_retirement_response', 'Central returned inconsistent retirement identity.', {
          recovery: 'Do not remove local state. Ask the operator to inspect the seat.',
        });
      }
    }
    const verified = await readStatus({}, 'disconnect');
    if (!verified.ok || verified.identity.lifecycle_status !== 'retired') {
      return failure('disconnect', 'retirement_not_verified', 'Central retirement was not verified.', {
        retryable: true,
        recovery: 'Keep the stored door and refresh live board status before retrying.',
      });
    }
    try {
      const forgotten = await dependencies.forgetDoor({ board: expectedBoard, role: identity.role });
      if (!forgotten || forgotten.ok !== true || forgotten.board !== expectedBoard || forgotten.role !== identity.role) {
        throw new Error('invalid forget result');
      }
    } catch (_error) {
      return result('disconnect', 'partial', {
        identity: verified.identity,
        code: 'local_disconnect_incomplete',
        message: 'Central retired the seat, but local door removal did not complete.',
        retryable: true,
        recovery: 'Retry disconnect with the same exact confirmation to remove the local door.',
        confirmation: current.confirmation,
      });
    }
    boundIdentity = null;
    return result('disconnect', 'disconnected', {
      identity: verified.identity,
      message: 'The seat is retired in Central and its local door is removed.',
      recovery: 'Rejoin with the same seat name to reactivate this identity.',
    });
  }

  return { disconnect, join, status };
}

module.exports = {
  confirmation,
  createSeatLifecycle,
  safeIdentity,
  sameIdentity,
};
