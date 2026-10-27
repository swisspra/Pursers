/* Optional finite read-only AG-UI observation client for Fleet views. */
(function installPursersAgUiClient() {
  'use strict';

  let nextRun = 1;
  const idPattern = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;

  function abortIfNeeded(signal) {
    if (signal?.aborted) throw new DOMException('The operation was aborted.', 'AbortError');
  }

  function parseFrames(body) {
    return body.split(/\r?\n\r?\n/).map(frame => {
      const lines = frame.split(/\r?\n/).filter(line => line.startsWith('data:'));
      if (!lines.length) return null;
      return JSON.parse(lines.map(line => line.slice(5).trimStart()).join('\n'));
    }).filter(Boolean);
  }

  function validateObservation(events, scope, threadId, runId) {
    if (events.length < 3 || events[0]?.type !== 'RUN_STARTED' || events.at(-1)?.type !== 'RUN_FINISHED') {
      throw new Error('Observation did not finish a finite AG-UI run.');
    }
    for (const event of [events[0], events.at(-1)]) {
      if (event.threadId !== threadId || event.runId !== runId) {
        throw new Error('Observation run scope did not match the request.');
      }
    }
    if (events[0].protocolVersion !== '1.0') throw new Error('Unsupported AG-UI protocol version.');
    const snapshots = events.slice(1, -1);
    if (!snapshots.length || snapshots[0].type !== 'STATE_SNAPSHOT' || snapshots.length > 2) {
      throw new Error('Observation did not include one state snapshot.');
    }
    if (snapshots.some(event => !['STATE_SNAPSHOT', 'ACTIVITY_SNAPSHOT'].includes(event.type))) {
      throw new Error('Observation included an unsupported event.');
    }
    const snapshot = snapshots[0].snapshot;
    if (!snapshot || snapshot.scope?.central !== scope.central || snapshot.scope?.board_id !== scope.board_id || !Array.isArray(snapshot.tickets)) {
      throw new Error('Observation snapshot scope did not match the requested project.');
    }
    if (snapshots[1]) {
      const content = snapshots[1].content;
      if (snapshots[1].type !== 'ACTIVITY_SNAPSHOT' || content?.scope?.central !== scope.central || content?.scope?.board_id !== scope.board_id || !Array.isArray(content.activities)) {
        throw new Error('Observation activity scope did not match the requested project.');
      }
      snapshot.activity = content;
    }
    if (!snapshot.coverage || typeof snapshot.coverage !== 'object' || Array.isArray(snapshot.coverage)) snapshot.coverage = {};
    return snapshot;
  }

  async function observe(scope, {signal} = {}) {
    if (!scope || !idPattern.test(String(scope.central || '')) || !idPattern.test(String(scope.board_id || ''))) {
      throw new TypeError('Observation requires a valid Central and board ID.');
    }
    abortIfNeeded(signal);
    const threadId = 'pursers-projects';
    const runId = `observe-${nextRun++}`;
    const response = await fetch(`/api/ag-ui/observe?central=${encodeURIComponent(scope.central)}&board_id=${encodeURIComponent(scope.board_id)}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'Accept': 'text/event-stream'},
      body: JSON.stringify({threadId, runId, protocolVersion: '1.0', messages: [], tools: [], context: [], state: {}}),
      signal,
    });
    abortIfNeeded(signal);
    if (!response.ok) throw new Error(`Observation unavailable (HTTP ${response.status}).`);
    const contentType = response.headers.get('content-type') || '';
    if (!contentType.toLowerCase().startsWith('text/event-stream')) throw new Error('Observation response was not an event stream.');
    const body = await response.text();
    abortIfNeeded(signal);
    return validateObservation(parseFrames(body), scope, threadId, runId);
  }

  globalThis.PursersAgUi = Object.freeze({observe});
})();
