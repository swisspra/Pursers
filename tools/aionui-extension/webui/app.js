'use strict';

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

async function readJson(response) {
  try {
    return await response.json();
  } catch (_error) {
    return { ok: false, error: 'invalid_response' };
  }
}

function createDoorPreview(result) {
  const metadata = result && result.metadata ? result.metadata : {};
  const normalized = result && result.normalized ? result.normalized : {};
  return [
    ['Door ID', valueOrDash(metadata.kid)],
    ['Central host', valueOrDash(metadata.central_host)],
    ['Board', valueOrDash(metadata.board)],
    ['Role', valueOrDash(metadata.role)],
    ['Seat name', valueOrDash(normalized.seat_name || 'Reserved on first session')],
    ['Tier', valueOrDash(normalized.tier_max)],
    ['Expires', metadata.exp ? new Date(Number(metadata.exp) * 1000).toLocaleString() : '—'],
    ['Transport', valueOrDash(metadata.transport)],
  ];
}

function renderDoorPreview(result, list, documentRef) {
  const fields = createDoorPreview(result);
  const children = [];
  for (const [label, value] of fields) {
    const term = documentRef.createElement('dt');
    term.textContent = label;
    const detail = documentRef.createElement('dd');
    detail.textContent = value;
    children.push(term, detail);
  }
  list.replaceChildren(...children);
}

function onboardingErrorMessage(result, stage) {
  if (result && result.code === 'expired_door') {
    return 'This door has expired. Ask your coordinator for a replacement door.';
  }
  if (result && result.code === 'invalid_door') {
    return 'This door is malformed. Ask your coordinator for a valid Pursers door.';
  }
  if (result && result.message) return result.message;
  return stage === 'validate'
    ? 'Door validation failed. Check the door and try again.'
    : 'Connect failed. Ask your coordinator to check the door.';
}

function initialize(documentRef, fetchImpl) {
  const form = documentRef.querySelector('#join-form');
  const doorInput = documentRef.querySelector('#door');
  const message = documentRef.querySelector('#message');
  const validateButton = documentRef.querySelector('#validate-door');
  const confirmation = documentRef.querySelector('#door-confirmation');
  const preview = documentRef.querySelector('#door-preview');
  const connectButton = documentRef.querySelector('#connect-door');
  const cancelButton = documentRef.querySelector('#cancel-door');
  let pendingDoor = null;
  const ui = {
    card: documentRef.querySelector('#status-card'),
    icon: documentRef.querySelector('#status-icon'),
    title: documentRef.querySelector('#status-title'),
    summary: documentRef.querySelector('#status-summary'),
    seatList: documentRef.querySelector('#seat-list'),
  };
  const showResult = (result) => renderStartupView(
    createStartupView(result), ui, documentRef,
  );
  const setBusy = (busy) => {
    form.setAttribute('aria-busy', String(busy));
    validateButton.disabled = busy;
    connectButton.disabled = busy;
  };
  const clearPending = () => {
    pendingDoor = null;
    confirmation.hidden = true;
    preview.replaceChildren();
  };

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const door = doorInput.value.trim();
    doorInput.value = '';
    clearPending();
    message.textContent = 'Validating door…';
    setBusy(true);
    try {
      const response = await fetchImpl('/pursers/onboarding/validate', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ door }),
      });
      const result = await readJson(response);
      if (!response.ok || !result.ok) {
        message.textContent = onboardingErrorMessage(result, 'validate');
        return;
      }
      pendingDoor = door;
      renderDoorPreview(result, preview, documentRef);
      confirmation.hidden = false;
      message.textContent = 'Door validated. Review the redacted details, then select Connect.';
    } catch (_error) {
      message.textContent = 'Door validation could not reach the local extension. Try again.';
    } finally {
      setBusy(false);
    }
  });

  connectButton.addEventListener('click', async () => {
    if (!pendingDoor) return;
    const door = pendingDoor;
    clearPending();
    message.textContent = 'Connecting…';
    setBusy(true);
    try {
      const response = await fetchImpl('/pursers/onboarding/connect', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ door }),
      });
      const result = await readJson(response);
      if (!response.ok || !result.ok) {
        message.textContent = onboardingErrorMessage(result, 'connect');
        return;
      }
      message.textContent = `Joined and registered ${result.mcp_server}.`;
      showResult({
        ok: true,
        push_mode: result.status && result.status.push_mode,
        seats: result.status ? [result.status] : [],
      });
    } catch (_error) {
      message.textContent = 'Connect could not reach the local extension. Validate the door again.';
    } finally {
      setBusy(false);
    }
  });

  cancelButton.addEventListener('click', () => {
    clearPending();
    message.textContent = 'Connection cancelled. Paste a door to start again.';
    doorInput.focus();
  });

  fetchImpl('/pursers/status')
    .then(readJson)
    .then(showResult)
    .catch(() => showResult({ ok: false, error: 'status_unavailable' }));
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    createDoorPreview,
    createStartupView,
    initialize,
    normalizeSeat,
    onboardingErrorMessage,
    renderDoorPreview,
    renderStartupView,
  };
}

if (typeof document !== 'undefined' && typeof fetch !== 'undefined') {
  initialize(document, fetch);
}
