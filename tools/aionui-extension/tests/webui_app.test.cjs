'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const manifest = require('../aion-extension.json');
const { createHandlers } = require('../webui/routes.js');
const {
  PRESETS_BY_ROLE,
  createPostJoinGuidance,
  createStartupView,
  initialize,
  renderPostJoinGuidance,
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
    this.listeners = {};
  }

  append(...children) {
    this.children.push(...children);
  }

  replaceChildren(...children) {
    this.children = children;
  }

  addEventListener(type, listener) {
    this.listeners[type] = listener;
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

function nextStepFixture() {
  return {
    card: new FakeElement('section'),
    title: new FakeElement('h2'),
    summary: new FakeElement('p'),
    presetList: new FakeElement('ul'),
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

test('post-join guidance uses the exact contributed preset labels', () => {
  const assistants = manifest.contributes.assistants;
  assert.deepEqual(
    [...PRESETS_BY_ROLE.worker, ...PRESETS_BY_ROLE.reviewer],
    assistants.map((assistant) => assistant.name),
  );

  const guidance = createPostJoinGuidance({ ok: true, status: { role: 'reviewer' } });
  const ui = nextStepFixture();
  renderPostJoinGuidance(guidance, ui, documentRef);
  assert.equal(ui.card.hidden, false);
  assert.equal(ui.title.textContent, 'Start a new conversation');
  assert.match(ui.summary.textContent, /reviewer preset/);
  assert.deepEqual(
    ui.presetList.children.map((item) => item.textContent),
    ['Pursers Reviewer (Codex)', 'Pursers Reviewer (Claude)'],
  );
});

test('post-join guidance stays hidden without a successful supported-role join', () => {
  const ui = nextStepFixture();
  for (const result of [null, { ok: false }, { ok: true }, { ok: true, status: { role: 'admin' } }]) {
    renderPostJoinGuidance(createPostJoinGuidance(result), ui, documentRef);
    assert.equal(ui.card.hidden, true);
    assert.equal(ui.presetList.children.length, 0);
  }
});

test('submit flow reveals guidance only after join success', async () => {
  const connectionCard = new FakeElement('section');
  connectionCard.querySelector = () => new FakeElement('span');
  const elements = {
    '#join-form': new FakeElement('form'),
    '#door': new FakeElement('input'),
    '#message': new FakeElement('p'),
    '#status-card': new FakeElement('section'),
    '#status-icon': new FakeElement('span'),
    '#status-title': new FakeElement('h2'),
    '#status-summary': new FakeElement('p'),
    '#seat-list': new FakeElement('div'),
    '#connection-card': connectionCard,
    '#connection-title': new FakeElement('h2'),
    '#recover': new FakeElement('button'),
    '#next-step': new FakeElement('section'),
    '#next-step-title': new FakeElement('h2'),
    '#next-step-summary': new FakeElement('p'),
    '#preset-list': new FakeElement('ul'),
  };
  const interactiveDocument = {
    ...documentRef,
    querySelector(selector) {
      return elements[selector];
    },
  };
  let joinResult = { ok: false, error: 'invalid_door' };
  const fetchImpl = async (url) => {
    if (url === '/pursers/status') {
      return new Response(JSON.stringify({ ok: true, push_mode: 'push', seats: [] }));
    }
    return new Response(JSON.stringify(joinResult), { status: joinResult.ok ? 200 : 400 });
  };
  initialize(interactiveDocument, fetchImpl);
  await new Promise((resolve) => setImmediate(resolve));

  elements['#door'].value = 'invalid';
  await elements['#join-form'].listeners.submit({ preventDefault() {} });
  assert.equal(elements['#next-step'].hidden, true);

  joinResult = {
    ok: true,
    mcp_server: 'Pursers worker demo',
    status: { board: 'demo', role: 'worker', seat_name: 'worker-one', push_mode: 'push' },
  };
  elements['#door'].value = 'synthetic';
  await elements['#join-form'].listeners.submit({ preventDefault() {} });
  assert.equal(elements['#next-step'].hidden, false);
  assert.deepEqual(
    elements['#preset-list'].children.map((item) => item.textContent),
    ['Pursers Worker (Codex)', 'Pursers Worker (Claude)'],
  );
});
