'use strict';

const PRESETS_BY_ROLE = Object.freeze({
  worker: Object.freeze(['Pursers Worker (Codex)', 'Pursers Worker (Claude)']),
  reviewer: Object.freeze(['Pursers Reviewer (Codex)', 'Pursers Reviewer (Claude)']),
});

function valueOrDash(value) {
  return value === undefined || value === null || value === '' ? '—' : String(value);
}

function normalizeSeat(seat, pushMode) {
  const names = Array.isArray(seat.seat_names)
    ? seat.seat_names
    : (seat.seat_name ? [seat.seat_name] : []);
  return {
    board: valueOrDash(seat.board),
    role: valueOrDash(seat.role),
    seat_name: names.join(', ') || 'Reserved on first session',
    push_mode: valueOrDash(seat.push_mode || pushMode),
    kid: valueOrDash(seat.kid),
    exp: seat.exp ? new Date(Number(seat.exp) * 1000).toLocaleString() : '—',
  };
}

function createStartupView(result) {
  if (!result || !result.ok) {
    if (result && result.error === 'bridge_not_installed') {
      return {
        state: 'bridge-missing',
        icon: '↓',
        title: 'Wait bridge missing',
        summary: result.install_hint || 'Install pursers-wait-bridge, then reload this page.',
        seats: [],
      };
    }
    return {
      state: 'unavailable',
      icon: '!',
      title: 'Status unavailable',
      summary: 'Pursers could not read wait-bridge status. Check the bridge, then reload this page.',
      seats: [],
    };
  }

  const seats = Array.isArray(result.seats)
    ? result.seats.map((seat) => normalizeSeat(seat, result.push_mode))
    : [];
  if (seats.length === 0) {
    return {
      state: 'empty',
      icon: '○',
      title: 'No connected seats',
      summary: 'No worker or reviewer seat is connected yet. Paste a door to get started.',
      seats,
    };
  }
  return {
    state: 'ready',
    icon: '✓',
    title: `${seats.length} connected seat${seats.length === 1 ? '' : 's'}`,
    summary: `Wait bridge status is available. Push mode: ${valueOrDash(result.push_mode)}.`,
    seats,
  };
}

function appendField(documentRef, list, label, value) {
  const term = documentRef.createElement('dt');
  term.textContent = label;
  const detail = documentRef.createElement('dd');
  detail.textContent = value;
  list.append(term, detail);
}

function renderStartupView(view, ui, documentRef) {
  ui.card.hidden = false;
  ui.card.dataset.state = view.state;
  ui.icon.textContent = view.icon;
  ui.title.textContent = view.title;
  ui.summary.textContent = view.summary;
  const cards = view.seats.map((seat) => {
    const article = documentRef.createElement('article');
    article.className = 'seat-card';
    const heading = documentRef.createElement('h3');
    heading.textContent = seat.seat_name;
    const list = documentRef.createElement('dl');
    appendField(documentRef, list, 'Board', seat.board);
    appendField(documentRef, list, 'Role', seat.role);
    appendField(documentRef, list, 'Seat name', seat.seat_name);
    appendField(documentRef, list, 'Push mode', seat.push_mode);
    appendField(documentRef, list, 'Key ID', seat.kid);
    appendField(documentRef, list, 'Expires', seat.exp);
    article.append(heading, list);
    return article;
  });
  ui.seatList.replaceChildren(...cards);
}

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

function createPostJoinGuidance(result) {
  if (!result || !result.ok || !result.status) return null;
  const role = String(result.status.role || '').toLowerCase();
  const presets = PRESETS_BY_ROLE[role];
  if (!presets) return null;
  return {
    title: 'Start a new conversation',
    summary: `Choose a matching ${role} preset:`,
    presets,
  };
}

function renderPostJoinGuidance(guidance, ui, documentRef) {
  ui.card.hidden = !guidance;
  ui.presetList.replaceChildren();
  if (!guidance) return;
  ui.title.textContent = guidance.title;
  ui.summary.textContent = guidance.summary;
  const items = guidance.presets.map((preset) => {
    const item = documentRef.createElement('li');
    item.textContent = preset;
    return item;
  });
  ui.presetList.replaceChildren(...items);
}

async function readJson(response) {
  try {
    return await response.json();
  } catch (_error) {
    return { ok: false, error: 'invalid_response' };
  }
}

function initialize(documentRef, fetchImpl) {
  const form = documentRef.querySelector('#join-form');
  const doorInput = documentRef.querySelector('#door');
  const message = documentRef.querySelector('#message');
  const ui = {
    card: documentRef.querySelector('#status-card'),
    icon: documentRef.querySelector('#status-icon'),
    title: documentRef.querySelector('#status-title'),
    summary: documentRef.querySelector('#status-summary'),
    seatList: documentRef.querySelector('#seat-list'),
  };
  const connectionCard = documentRef.querySelector('#connection-card');
  const connectionTitle = documentRef.querySelector('#connection-title');
  const recoverButton = documentRef.querySelector('#recover');
  let recoveryTarget = null;
  const nextStepUi = {
    card: documentRef.querySelector('#next-step'),
    title: documentRef.querySelector('#next-step-title'),
    summary: documentRef.querySelector('#next-step-summary'),
    presetList: documentRef.querySelector('#preset-list'),
  };
  const showResult = (result) => renderStartupView(
    createStartupView(result), ui, documentRef,
  );

  function showStatus(status, pushMode) {
    showResult({
      ok: true,
      push_mode: status.push_mode || pushMode,
      seats: [status],
    });
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

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const door = doorInput.value.trim();
    doorInput.value = '';
    message.textContent = 'Joining…';
    renderPostJoinGuidance(null, nextStepUi, documentRef);
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
      else if (result.error === 'bridge_not_installed') showResult(result);
      message.textContent = (state && state.message)
        || result.install_hint
        || 'Join failed. Ask your coordinator to check the door.';
      return;
    }
    showConnection(state);
    message.textContent = `Joined and registered ${result.mcp_server}.`;
    showStatus(result.status, result.push_mode);
    renderPostJoinGuidance(createPostJoinGuidance(result), nextStepUi, documentRef);
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
      showResult(result);
      if (!result.ok) {
        const state = connectionState(result);
        showConnection(state);
        if (state) message.textContent = state.message;
      }
    })
    .catch(() => showResult({ ok: false, error: 'status_unavailable' }));
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    PRESETS_BY_ROLE,
    connectionState,
    createPostJoinGuidance,
    createStartupView,
    initialize,
    mount: initialize,
    normalizeSeat,
    recoveryPayload,
    renderPostJoinGuidance,
    renderStartupView,
  };
}

if (typeof document !== 'undefined' && typeof fetch !== 'undefined') {
  initialize(document, fetch);
}
