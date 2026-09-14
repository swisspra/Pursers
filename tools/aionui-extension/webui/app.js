'use strict';

const MAX_JOIN_ATTEMPTS = 3;
const RETRY_COUNTDOWN_SECONDS = 3;

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

function joinFailure(response, result, transportError) {
  const code = result && (result.error || result.code);
  if (transportError) {
    return {
      retryable: true,
      title: 'Pursers helper is unreachable.',
      action: 'Keep AionUi open or restart it.',
    };
  }
  if (response.status === 401 || response.status === 403 || code === 'auth_rejected') {
    return {
      retryable: false,
      title: 'Authentication was rejected.',
      action: 'Sign in to AionUi again and reload this page.',
    };
  }
  if (code === 'server_unreachable') {
    return {
      retryable: true,
      title: 'Central is unreachable.',
      action: 'Check the Central URL and network.',
    };
  }
  if (code === 'bridge_status_failed') {
    return {
      retryable: true,
      title: 'Pursers helper status is unavailable.',
      action: 'Restart the wait bridge.',
    };
  }
  return {
    retryable: false,
    title: (result && result.install_hint) || 'Join failed.',
    action: 'Ask your coordinator to check the door.',
  };
}

function failureMessage(failure, attempts) {
  return `${failure.title} ${failure.action} Automatic retries stopped after ${attempts} attempts; paste the door again to retry.`;
}

async function waitForRetry(failure, nextAttempt, message, sleep) {
  for (let seconds = RETRY_COUNTDOWN_SECONDS; seconds > 0; seconds -= 1) {
    message.textContent = `${failure.title} ${failure.action} Retrying in ${seconds}s (attempt ${nextAttempt} of ${MAX_JOIN_ATTEMPTS}).`;
    await sleep(1000);
  }
  message.textContent = `Retrying… (attempt ${nextAttempt} of ${MAX_JOIN_ATTEMPTS})`;
}

function initialize(documentRef, fetchImpl, dependencies = {}) {
  const sleep = dependencies.sleep || ((milliseconds) => new Promise((resolve) => {
    setTimeout(resolve, milliseconds);
  }));
  const form = documentRef.querySelector('#join-form');
  const doorInput = documentRef.querySelector('#door');
  const submitButton = form.querySelector('button[type="submit"]');
  const message = documentRef.querySelector('#message');
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
  let joining = false;
  let viewGeneration = 0;

  const setBusy = (busy) => {
    joining = busy;
    form.setAttribute('aria-busy', busy ? 'true' : 'false');
    doorInput.disabled = busy;
    submitButton.disabled = busy;
    submitButton.textContent = busy ? 'Joining…' : 'Join';
  };

  const submitJoin = async (event) => {
    event.preventDefault();
    if (joining) return;
    const door = doorInput.value.trim();
    doorInput.value = '';
    viewGeneration += 1;
    setBusy(true);
    message.textContent = `Joining… (attempt 1 of ${MAX_JOIN_ATTEMPTS})`;
    try {
      for (let attempt = 1; attempt <= MAX_JOIN_ATTEMPTS; attempt += 1) {
        let response;
        let result;
        let transportError;
        try {
          response = await fetchImpl('/pursers/join', {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ door }),
          });
          result = await readJson(response);
        } catch (error) {
          transportError = error;
        }

        if (response && response.ok && result.ok) {
          message.textContent = `Joined and registered ${result.mcp_server}.`;
          showResult({
            ok: true,
            push_mode: result.status && result.status.push_mode,
            seats: result.status ? [result.status] : [],
          });
          return;
        }

        const failure = joinFailure(response || { status: 0 }, result, transportError);
        if (!failure.retryable || attempt === MAX_JOIN_ATTEMPTS) {
          message.textContent = failure.retryable
            ? failureMessage(failure, attempt)
            : `${failure.title} ${failure.action}`;
          if (result && result.error === 'bridge_not_installed') showResult(result);
          return;
        }
        await waitForRetry(failure, attempt + 1, message, sleep);
      }
    } finally {
      setBusy(false);
    }
  };

  form.addEventListener('submit', submitJoin);

  const startupGeneration = viewGeneration;
  const statusReady = fetchImpl('/pursers/status')
    .then(readJson)
    .then((result) => {
      if (viewGeneration === startupGeneration) showResult(result);
    })
    .catch(() => {
      if (viewGeneration === startupGeneration) {
        showResult({ ok: false, error: 'status_unavailable' });
      }
    });
  return { statusReady, submitJoin };
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    MAX_JOIN_ATTEMPTS,
    RETRY_COUNTDOWN_SECONDS,
    createStartupView,
    initialize,
    joinFailure,
    normalizeSeat,
    renderStartupView,
  };
}

if (typeof document !== 'undefined' && typeof fetch !== 'undefined') {
  initialize(document, fetch);
}
