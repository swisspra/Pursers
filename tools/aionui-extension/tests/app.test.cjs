'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { connectionState, mount, recoveryPayload } = require('../webui/app.js');

function uiFixture() {
  const listeners = new Map();
  const connections = Object.fromEntries(
    ['central', 'board', 'helper'].map((name) => [name, { textContent: '' }]),
  );
  class FakeElement {
    constructor() {
      this.children = [];
      this.className = '';
      this.dataset = {};
      this.hidden = true;
      this.textContent = '';
    }

    append(...children) {
      this.children.push(...children);
    }

    replaceChildren(...children) {
      this.children = children;
    }
  }
  const elements = {
    '#join-form': {
      attributes: {},
      addEventListener: (name, handler) => listeners.set(`form:${name}`, handler),
      setAttribute(name, value) { this.attributes[name] = value; },
    },
    '#door': { value: '', focus() {} },
    '#message': { textContent: '' },
    '#validate-door': { disabled: false },
    '#door-confirmation': { hidden: true },
    '#door-preview': new FakeElement(),
    '#connect-door': {
      disabled: false,
      addEventListener: (name, handler) => listeners.set(`connect:${name}`, handler),
    },
    '#cancel-door': {
      addEventListener: (name, handler) => listeners.set(`cancel:${name}`, handler),
    },
    '#status-card': new FakeElement(),
    '#status-icon': new FakeElement(),
    '#status-title': new FakeElement(),
    '#status-summary': new FakeElement(),
    '#seat-list': new FakeElement(),
    '#connection-card': {
      hidden: true,
      querySelector: (selector) => connections[selector.match(/data-connection=\"([^\"]+)/)[1]],
    },
    '#connection-title': { textContent: '' },
    '#recover': {
      hidden: true,
      disabled: false,
      addEventListener: (name, handler) => listeners.set(`recover:${name}`, handler),
    },
  };
  return {
    connections,
    elements,
    listeners,
    document: {
      createElement: () => new FakeElement(),
      querySelector: (selector) => elements[selector],
    },
  };
}

function jsonResponse(body, ok = true) {
  return { ok, json: async () => body };
}

async function flushPromises() {
  await new Promise((resolve) => setImmediate(resolve));
}

test('partial registration shows connected Central and board with door-free recovery', () => {
  const secretDoor = 'prs1.secret-must-not-survive';
  const state = connectionState({
    ok: false,
    outcome: 'partial',
    code: 'mcp_registration_failed',
    connected: true,
    status: {
      board: 'demo',
      role: 'worker',
      seat_name: 'worker-one',
      push_mode: 'push',
      kid: 'door-1',
      exp: 2000000000,
    },
    discarded_door: secretDoor,
  });

  assert.equal(state.title, 'Connected; registration incomplete');
  assert.equal(state.central, 'Connected');
  assert.equal(state.board, 'Joined');
  assert.equal(state.helper, 'Registration incomplete');
  assert.deepEqual(state.recovery, {
    board: 'demo',
    role: 'worker',
    seat_name: 'worker-one',
    tier_max: 2,
  });
  assert.equal(JSON.stringify(state.recovery).includes(secretDoor), false);
});

test('board refusal distinguishes reached Central from an unjoined board', () => {
  const state = connectionState({
    ok: false,
    error: 'bridge_rejected',
    message: 'The bridge rejected the door.',
  });

  assert.equal(state.title, 'Board join refused');
  assert.equal(state.central, 'Reached');
  assert.equal(state.board, 'Join refused');
  assert.equal(state.helper, 'Not registered');
  assert.equal(state.recovery, null);
});

test('missing or down helper stays distinct from Central and board state', () => {
  const missing = connectionState({
    ok: false,
    error: 'bridge_not_installed',
    install_hint: 'Install the bridge.',
  });
  const down = connectionState({
    ok: false,
    code: 'bridge_status_failed',
    message: 'Stored door status is unavailable.',
  });

  assert.deepEqual(
    [missing.central, missing.board, missing.helper],
    ['Not checked', 'Not joined', 'Not installed'],
  );
  assert.deepEqual(
    [down.central, down.board, down.helper],
    ['Not checked', 'Not joined', 'Unavailable'],
  );
  assert.equal(missing.recovery, null);
  assert.equal(down.recovery, null);
});

test('recovery payload refuses incomplete redacted status', () => {
  assert.equal(recoveryPayload(null), null);
  assert.equal(recoveryPayload({ board: 'demo', role: 'worker' }), null);
});

test('mounted partial result exposes Recover and posts only the redacted target', async () => {
  const fixture = uiFixture();
  const requests = [];
  const status = {
    board: 'demo', role: 'worker', seat_name: 'worker-one', push_mode: 'push', kid: 'door-1', exp: 2000000000,
  };
  const fetchImpl = async (url, options = {}) => {
    requests.push({ url, options });
    if (url === '/pursers/status') return jsonResponse({ ok: true, seats: [] });
    if (url === '/pursers/onboarding/validate') {
      return jsonResponse({
        ok: true,
        metadata: { board: 'demo', role: 'worker', kid: 'door-1' },
        normalized: { seat_name: 'worker-one', tier_max: 2 },
      });
    }
    if (url === '/pursers/onboarding/connect') {
      return jsonResponse({ ok: false, error: 'mcp_registration_failed', outcome: 'partial', joined: true, status }, false);
    }
    return jsonResponse({ ok: true, outcome: 'recovered', status });
  };
  mount(fixture.document, fetchImpl);
  await flushPromises();
  fixture.elements['#door'].value = 'prs1.secret-must-not-survive';
  await fixture.listeners.get('form:submit')({ preventDefault() {} });
  await fixture.listeners.get('connect:click')();

  assert.equal(fixture.elements['#connection-title'].textContent, 'Connected; registration incomplete');
  assert.deepEqual(
    Object.values(fixture.connections).map((element) => element.textContent),
    ['Connected', 'Joined', 'Registration incomplete'],
  );
  assert.equal(fixture.elements['#recover'].hidden, false);
  await fixture.listeners.get('recover:click')();

  const recoveryRequest = requests.find((request) => request.url === '/pursers/onboarding/recover');
  assert.deepEqual(JSON.parse(recoveryRequest.options.body), {
    board: 'demo', role: 'worker', seat_name: 'worker-one', tier_max: 2,
  });
  assert.equal(recoveryRequest.options.body.includes('secret-must-not-survive'), false);
  assert.equal(fixture.elements['#message'].textContent, 'Recovered and registered without re-entering the door.');
});

test('mounted startup status exposes a down helper instead of an empty screen', async () => {
  const fixture = uiFixture();
  mount(fixture.document, async () => jsonResponse({ ok: false, error: 'bridge_status_failed' }, false));
  await flushPromises();

  assert.equal(fixture.elements['#connection-card'].hidden, false);
  assert.equal(fixture.elements['#connection-title'].textContent, 'Local helper unavailable');
  assert.deepEqual(
    Object.values(fixture.connections).map((element) => element.textContent),
    ['Not checked', 'Not joined', 'Unavailable'],
  );
});
