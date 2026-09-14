'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { createHandlers } = require('../webui/routes.js');
const {
  createDoorPreview,
  createStartupView,
  initialize,
  onboardingErrorMessage,
  renderStartupView,
} = require('../webui/app.js');

class FakeElement {
  constructor(tagName = 'div') {
    this.tagName = tagName;
    this.children = [];
    this.dataset = {};
    this.hidden = true;
    this.textContent = '';
    this.className = '';
    this.value = '';
    this.disabled = false;
    this.attributes = {};
    this.listeners = {};
    this.focused = false;
  }

  append(...children) {
    this.children.push(...children);
  }

  replaceChildren(...children) {
    this.children = children;
  }

  addEventListener(name, listener) {
    this.listeners[name] = listener;
  }

  setAttribute(name, value) {
    this.attributes[name] = value;
  }

  focus() {
    this.focused = true;
  }

  async trigger(name, event = {}) {
    return this.listeners[name]({ preventDefault() {}, ...event });
  }
}

const documentRef = {
  createElement(tagName) {
    return new FakeElement(tagName);
  },
};

function uiFixture() {
  return {
    card: new FakeElement('section'),
    icon: new FakeElement('span'),
    title: new FakeElement('h2'),
    summary: new FakeElement('p'),
    seatList: new FakeElement('div'),
  };
}

async function statusPayload(runBridge) {
  const handlers = createHandlers({ runBridge });
  const response = await handlers.handle(new Request('http://localhost/pursers/status'));
  return { response, payload: await response.json() };
}

test('empty startup state has distinct copy and renders no seat cards', async () => {
  const { response, payload } = await statusPayload(async () => 'push_mode=push\n');
  assert.equal(response.status, 200);
  const view = createStartupView(payload);
  const ui = uiFixture();
  renderStartupView(view, ui, documentRef);
  assert.deepEqual(
    { state: view.state, icon: view.icon, title: view.title, cards: ui.seatList.children.length },
    { state: 'empty', icon: '○', title: 'No connected seats', cards: 0 },
  );
  assert.match(ui.summary.textContent, /Paste a door/);
});

test('bridge-missing startup state preserves the bounded install hint', async () => {
  const missing = Object.assign(new Error('not found'), { code: 'ENOENT' });
  const { response, payload } = await statusPayload(async () => { throw missing; });
  assert.equal(response.status, 503);
  const view = createStartupView(payload);
  const ui = uiFixture();
  renderStartupView(view, ui, documentRef);
  assert.equal(ui.card.dataset.state, 'bridge-missing');
  assert.equal(ui.icon.textContent, '↓');
  assert.equal(ui.title.textContent, 'Wait bridge missing');
  assert.match(ui.summary.textContent, /uv tool install/);
});

test('unavailable startup state differs from empty and bridge-missing', async () => {
  const { response, payload } = await statusPayload(async () => { throw new Error('offline'); });
  assert.equal(response.status, 502);
  const view = createStartupView(payload);
  const ui = uiFixture();
  renderStartupView(view, ui, documentRef);
  assert.deepEqual(
    { state: ui.card.dataset.state, icon: ui.icon.textContent, title: ui.title.textContent },
    { state: 'unavailable', icon: '!', title: 'Status unavailable' },
  );
  assert.equal(new Set(['○', '↓', ui.icon.textContent]).size, 3);
});

test('one connected seat renders one complete seat card', async () => {
  const { payload } = await statusPayload(async () => (
    'push_mode=push\nboard=alpha role=worker kid=door-a exp=2000000000 seat_names_used=worker-one\n'
  ));
  const view = createStartupView(payload);
  const ui = uiFixture();
  renderStartupView(view, ui, documentRef);
  assert.equal(view.title, '1 connected seat');
  assert.equal(ui.seatList.children.length, 1);
  assert.equal(ui.seatList.children[0].children[0].textContent, 'worker-one');
  assert.equal(ui.seatList.children[0].children[1].children[1].textContent, 'alpha');
});

test('multiple product status rows render every connected seat', async () => {
  const { payload } = await statusPayload(async () => [
    'push_mode=push',
    'board=alpha role=worker kid=door-a exp=2000000000 seat_names_used=worker-one',
    'board=beta role=reviewer kid=door-b exp=2000000001 seat_names_used=reviewer-one,reviewer-two',
    '',
  ].join('\n'));
  const view = createStartupView(payload);
  const ui = uiFixture();
  renderStartupView(view, ui, documentRef);
  assert.equal(view.title, '2 connected seats');
  assert.equal(ui.seatList.children.length, 2);
  assert.deepEqual(
    ui.seatList.children.map((seatCard) => seatCard.children[0].textContent),
    ['worker-one', 'reviewer-one, reviewer-two'],
  );
});

test('join status shape normalizes into the same ready view', () => {
  const view = createStartupView({
    ok: true,
    push_mode: 'push',
    seats: [{ board: 'alpha', role: 'worker', seat_name: 'worker-one', kid: 'door-a', exp: 2000000000 }],
  });
  assert.equal(view.state, 'ready');
  assert.equal(view.icon, '✓');
  assert.equal(view.seats[0].seat_name, 'worker-one');
});

test('door preview contains only redacted confirmation metadata', () => {
  const fields = Object.fromEntries(createDoorPreview({
    metadata: {
      board: 'alpha', role: 'worker', kid: 'door-a', exp: 2000000000,
      central_host: 'central.example', transport: 'https',
    },
    normalized: { seat_name: null, tier_max: 2 },
  }));
  assert.equal(fields['Door ID'], 'door-a');
  assert.equal(fields['Central host'], 'central.example');
  assert.equal(fields.Board, 'alpha');
  assert.equal(fields['Seat name'], 'Reserved on first session');
  assert.equal(fields.Tier, '2');
  assert.equal(JSON.stringify(fields).includes('prs1.'), false);
});

test('validation requires a second explicit Connect before connection', async () => {
  const ids = [
    'join-form', 'door', 'message', 'validate-door', 'door-confirmation',
    'door-preview', 'connect-door', 'cancel-door', 'status-card',
    'status-icon', 'status-title', 'status-summary', 'seat-list',
  ];
  const elements = Object.fromEntries(ids.map((id) => [id, new FakeElement()]));
  elements.door.value = 'prs1.secret-door';
  const fakeDocument = {
    createElement: documentRef.createElement,
    querySelector(selector) { return elements[selector.slice(1)]; },
  };
  const calls = [];
  const fetchImpl = async (path, options = {}) => {
    calls.push({ path, options });
    if (path === '/pursers/status') {
      return new Response(JSON.stringify({ ok: true, push_mode: 'push', seats: [] }));
    }
    if (path === '/pursers/onboarding/validate') {
      return new Response(JSON.stringify({
        ok: true,
        metadata: {
          board: 'alpha', role: 'worker', kid: 'door-a', exp: 2000000000,
          central_host: 'central.example', transport: 'https',
        },
        normalized: { seat_name: null, tier_max: 2 },
      }));
    }
    return new Response(JSON.stringify({
      ok: true,
      mcp_server: 'pursers-alpha-worker',
      status: { board: 'alpha', role: 'worker', seat_name: 'worker-one' },
    }));
  };

  initialize(fakeDocument, fetchImpl);
  await elements['join-form'].trigger('submit');
  const onboardingCalls = calls.filter((call) => call.path.includes('/onboarding/'));
  assert.deepEqual(onboardingCalls.map((call) => call.path), ['/pursers/onboarding/validate']);
  assert.equal(elements['door-confirmation'].hidden, false);
  assert.equal(elements.door.value, '');
  assert.equal(elements['door-preview'].children.some((child) => child.textContent.includes('secret-door')), false);
  assert.match(elements.message.textContent, /select Connect/);

  await elements['connect-door'].trigger('click');
  assert.deepEqual(
    calls.filter((call) => call.path.includes('/onboarding/')).map((call) => call.path),
    ['/pursers/onboarding/validate', '/pursers/onboarding/connect'],
  );
  assert.equal(JSON.parse(calls.at(-1).options.body).door, 'prs1.secret-door');
  assert.match(elements.message.textContent, /Joined and registered/);
});

test('malformed and expired doors have distinct actionable copy', () => {
  assert.match(onboardingErrorMessage({ code: 'invalid_door' }, 'validate'), /malformed/);
  assert.match(onboardingErrorMessage({ code: 'expired_door' }, 'validate'), /expired/);
  assert.notEqual(
    onboardingErrorMessage({ code: 'invalid_door' }, 'validate'),
    onboardingErrorMessage({ code: 'expired_door' }, 'validate'),
  );
});
