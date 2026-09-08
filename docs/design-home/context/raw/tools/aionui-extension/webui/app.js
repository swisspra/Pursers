'use strict';

const form = document.querySelector('#join-form');
const doorInput = document.querySelector('#door');
const message = document.querySelector('#message');
const card = document.querySelector('#status-card');

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
  const response = await fetch('/pursers/join', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ door }),
  });
  const result = await readJson(response);
  if (!response.ok || !result.ok) {
    message.textContent = result.install_hint || 'Join failed. Ask your coordinator to check the door.';
    return;
  }
  message.textContent = `Joined and registered ${result.mcp_server}.`;
  showStatus(result.status);
});

fetch('/pursers/status')
  .then(readJson)
  .then((result) => {
    if (!result.ok || !result.seats || result.seats.length === 0) return;
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
