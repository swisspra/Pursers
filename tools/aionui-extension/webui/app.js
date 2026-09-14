'use strict';

function recoveryPayload(status) {
  if (!status || typeof status !== 'object') return null;
  const { board, role, seat_name: seatName } = status;
  if (![board, role, seatName].every((value) => typeof value === 'string' && value)) return null;
  return { board, role, seat_name: seatName, tier_max: 2 };
}

function connectionState(result) {
  const value = result && typeof result === 'object' ? result : {};
  const code = value.code || value.error;
  if (value.outcome === 'partial' || value.connected || value.joined) {
    const recovery = recoveryPayload(value.status);
    return {
      title: 'Connected; registration incomplete',
      central: 'Connected',
      board: 'Joined',
      helper: 'Registration incomplete',
      message: recovery
        ? 'Your seat is stored. Recover registers the helper without asking for the door again.'
        : 'Your seat is stored, but its recovery details are unavailable. Check bridge status and retry.',
      recovery,
    };
  }
  if (value.ok && value.status) {
    return {
      title: 'Connected and registered',
      central: 'Connected',
      board: 'Joined',
      helper: 'Registered',
      message: '',
      recovery: null,
    };
  }
  if (code === 'bridge_rejected' || code === 'identity_conflict' || code === 'rotation_required') {
    return {
      title: 'Board join refused',
      central: 'Reached',
      board: 'Join refused',
      helper: 'Not registered',
      message: value.message || 'The seat was not stored. Check the board, role, and door with your coordinator.',
      recovery: null,
    };
  }
  if (code === 'bridge_not_installed' || code === 'bridge_status_failed') {
    return {
      title: 'Local helper unavailable',
      central: 'Not checked',
      board: 'Not joined',
      helper: code === 'bridge_not_installed' ? 'Not installed' : 'Unavailable',
      message: value.install_hint || value.message || 'Restore pursers-wait-bridge, then try again.',
      recovery: null,
    };
  }
  if (code === 'server_unreachable') {
    return {
      title: 'Central unavailable',
      central: 'Unavailable',
      board: 'Not joined',
      helper: 'Not registered',
      message: value.message || 'Check the Central connection, then try again.',
      recovery: null,
    };
  }
  return null;
}

function mount(doc = document, fetchImpl = fetch) {
  const form = doc.querySelector('#join-form');
  const doorInput = doc.querySelector('#door');
  const message = doc.querySelector('#message');
  const card = doc.querySelector('#status-card');
  const connectionCard = doc.querySelector('#connection-card');
  const connectionTitle = doc.querySelector('#connection-title');
  const recoverButton = doc.querySelector('#recover');
  let recoveryTarget = null;

  function showStatus(status) {
    for (const [field, value] of Object.entries(status)) {
      const target = card.querySelector(`[data-field="${field}"]`);
      if (!target) continue;
      target.textContent = field === 'exp' && value
        ? new Date(Number(value) * 1000).toLocaleString()
        : String(value || '—');
    }
    card.hidden = false;
  }

  function showConnection(state) {
    if (!state) return;
    connectionTitle.textContent = state.title;
    for (const field of ['central', 'board', 'helper']) {
      connectionCard.querySelector(`[data-connection="${field}"]`).textContent = state[field];
    }
    recoveryTarget = state.recovery;
    recoverButton.hidden = !recoveryTarget;
    connectionCard.hidden = false;
  }

  async function readJson(response) {
    try {
      return await response.json();
    } catch (_error) {
      return { ok: false, error: 'invalid_response' };
    }
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const door = doorInput.value.trim();
    doorInput.value = '';
    message.textContent = 'Joining…';
    const response = await fetchImpl('/pursers/join', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ door }),
    });
    const result = await readJson(response);
    const state = connectionState(result);
    if (!response.ok || !result.ok) {
      showConnection(state);
      if (result.status) showStatus(result.status);
      message.textContent = (state && state.message)
        || result.install_hint
        || 'Join failed. Ask your coordinator to check the door.';
      return;
    }
    showConnection(state);
    message.textContent = `Joined and registered ${result.mcp_server}.`;
    showStatus(result.status);
  });

  recoverButton.addEventListener('click', async () => {
    if (!recoveryTarget) return;
    recoverButton.disabled = true;
    message.textContent = 'Recovering…';
    try {
      const response = await fetchImpl('/pursers/onboarding/recover', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(recoveryTarget),
      });
      const result = await readJson(response);
      const state = connectionState(result);
      showConnection(state);
      if (result.status) showStatus(result.status);
      message.textContent = response.ok && result.ok
        ? 'Recovered and registered without re-entering the door.'
        : (state && state.message) || 'Recovery failed. Check the helper and try again.';
    } catch (_error) {
      message.textContent = 'Recovery could not reach the local helper. Try again.';
    } finally {
      recoverButton.disabled = false;
    }
  });

  fetchImpl('/pursers/status')
    .then(readJson)
    .then((result) => {
      if (!result.ok) {
        const state = connectionState(result);
        showConnection(state);
        if (state) message.textContent = state.message;
        return;
      }
      if (!result.seats || result.seats.length === 0) return;
      const seat = result.seats[0];
      showStatus({
        board: seat.board,
        role: seat.role,
        seat_name: seat.seat_names.join(', ') || 'reserved on first session',
        push_mode: result.push_mode,
        kid: seat.kid,
        exp: seat.exp,
      });
    })
    .catch(() => {});
}

if (typeof document !== 'undefined') mount();
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { connectionState, mount, recoveryPayload };
}
