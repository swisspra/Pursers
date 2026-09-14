'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { createHandlers } = require('../webui/routes.js');
const {
  MAX_JOIN_ATTEMPTS,
  createStartupView,
  initialize,
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
    this.disabled = false;
    this.value = '';
    this.attributes = {};
    this.listeners = {};
    this.queries = {};
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

  querySelector(selector) {
    return this.queries[selector];
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

function appDocument() {
  const elements = {
    '#join-form': new FakeElement('form'),
    '#door': new FakeElement('input'),
    '#message': new FakeElement('p'),
    '#status-card': new FakeElement('section'),
    '#status-icon': new FakeElement('span'),
    '#status-title': new FakeElement('h2'),
    '#status-summary': new FakeElement('p'),
    '#seat-list': new FakeElement('div'),
  };
  elements.submit = new FakeElement('button');
  elements.submit.textContent = 'Join';
  elements['#join-form'].queries['button[type="submit"]'] = elements.submit;
  return {
    elements,
    createElement: documentRef.createElement,
    querySelector(selector) {
      return elements[selector];
    },
  };
}

function response(status, payload) {
  return { ok: status >= 200 && status < 300, status, json: async () => payload };
}

function statusResponse() {
  return response(200, { ok: true, push_mode: 'push', seats: [] });
}

const submitEvent = { preventDefault() {} };

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

test('helper transport failures retry three times with a visible countdown', async () => {
  const document = appDocument();
  const messagesAtTicks = [];
  let joinCalls = 0;
  const controller = initialize(document, async (url) => {
    if (url === '/pursers/status') return statusResponse();
    joinCalls += 1;
    throw new TypeError('synthetic connection failure');
  }, {
    sleep: async () => {
      assert.equal(document.elements['#join-form'].attributes['aria-busy'], 'true');
      assert.equal(document.elements.submit.disabled, true);
      messagesAtTicks.push(document.elements['#message'].textContent);
    },
  });
  await controller.statusReady;
  document.elements['#door'].value = 'secret-door';
  await controller.submitJoin(submitEvent);

  assert.equal(joinCalls, MAX_JOIN_ATTEMPTS);
  assert.equal(messagesAtTicks.length, 6);
  assert.match(messagesAtTicks[0], /Retrying in 3s \(attempt 2 of 3\)/);
  assert.match(messagesAtTicks[3], /Retrying in 3s \(attempt 3 of 3\)/);
  assert.match(document.elements['#message'].textContent, /helper is unreachable/);
  assert.match(document.elements['#message'].textContent, /stopped after 3 attempts/);
  assert.equal(document.elements['#message'].textContent.includes('secret-door'), false);
  assert.equal(document.elements['#join-form'].attributes['aria-busy'], 'false');
  assert.equal(document.elements['#door'].disabled, false);
  assert.equal(document.elements.submit.disabled, false);
  assert.equal(document.elements.submit.textContent, 'Join');
});

test('Central failures retry and retain one busy state until success', async () => {
  const document = appDocument();
  let joinCalls = 0;
  const controller = initialize(document, async (url) => {
    if (url === '/pursers/status') return statusResponse();
    joinCalls += 1;
    if (joinCalls < 3) return response(502, { ok: false, error: 'server_unreachable' });
    return response(200, {
      ok: true,
      mcp_server: 'Pursers worker demo',
      status: { board: 'demo', role: 'worker', seat_name: 'worker-one', push_mode: 'push' },
    });
  }, { sleep: async () => {} });
  await controller.statusReady;
  document.elements['#door'].value = 'secret-door';
  await controller.submitJoin(submitEvent);

  assert.equal(joinCalls, 3);
  assert.equal(document.elements['#message'].textContent, 'Joined and registered Pursers worker demo.');
  assert.equal(document.elements['#status-card'].dataset.state, 'ready');
  assert.equal(document.elements.submit.disabled, false);
});

test('authentication rejection is actionable and is not retried', async () => {
  const document = appDocument();
  let joinCalls = 0;
  let sleepCalls = 0;
  const controller = initialize(document, async (url) => {
    if (url === '/pursers/status') return statusResponse();
    joinCalls += 1;
    return response(401, { ok: false, error: 'unauthorized' });
  }, { sleep: async () => { sleepCalls += 1; } });
  await controller.statusReady;
  document.elements['#door'].value = 'secret-door';
  await controller.submitJoin(submitEvent);

  assert.equal(joinCalls, 1);
  assert.equal(sleepCalls, 0);
  assert.match(document.elements['#message'].textContent, /Authentication was rejected/);
  assert.match(document.elements['#message'].textContent, /Sign in to AionUi again/);
  assert.equal(document.elements.submit.disabled, false);
});

test('late startup status cannot replace a completed join view', async () => {
  const document = appDocument();
  let resolveStatus;
  const pendingStatus = new Promise((resolve) => { resolveStatus = resolve; });
  const controller = initialize(document, async (url) => {
    if (url === '/pursers/status') return pendingStatus;
    return response(200, {
      ok: true,
      mcp_server: 'Pursers worker demo',
      status: { board: 'demo', role: 'worker', seat_name: 'worker-one', push_mode: 'push' },
    });
  }, { sleep: async () => {} });
  document.elements['#door'].value = 'secret-door';
  await controller.submitJoin(submitEvent);
  assert.equal(document.elements['#status-card'].dataset.state, 'ready');

  resolveStatus(statusResponse());
  await controller.statusReady;
  assert.equal(document.elements['#status-card'].dataset.state, 'ready');
  assert.equal(document.elements['#status-title'].textContent, '1 connected seat');
});
