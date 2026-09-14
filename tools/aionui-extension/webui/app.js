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
  const nextStepUi = {
    card: documentRef.querySelector('#next-step'),
    title: documentRef.querySelector('#next-step-title'),
    summary: documentRef.querySelector('#next-step-summary'),
    presetList: documentRef.querySelector('#preset-list'),
  };
  const showResult = (result) => renderStartupView(
    createStartupView(result), ui, documentRef,
  );

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
    if (!response.ok || !result.ok) {
      message.textContent = result.install_hint || 'Join failed. Ask your coordinator to check the door.';
      if (result.error === 'bridge_not_installed') showResult(result);
      return;
    }
    message.textContent = `Joined and registered ${result.mcp_server}.`;
    renderPostJoinGuidance(createPostJoinGuidance(result), nextStepUi, documentRef);
    showResult({
      ok: true,
      push_mode: result.status && result.status.push_mode,
      seats: result.status ? [result.status] : [],
    });
  });

  fetchImpl('/pursers/status')
    .then(readJson)
    .then(showResult)
    .catch(() => showResult({ ok: false, error: 'status_unavailable' }));
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    PRESETS_BY_ROLE,
    createPostJoinGuidance,
    createStartupView,
    initialize,
    normalizeSeat,
    renderPostJoinGuidance,
    renderStartupView,
  };
}

if (typeof document !== 'undefined' && typeof fetch !== 'undefined') {
  initialize(document, fetch);
}
