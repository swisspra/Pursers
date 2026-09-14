import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import {
  ticketBlocker,
  ticketNow,
  ticketReviewLabel,
  ticketWorkStage,
  workCounts,
} from './work-state.js';

const sourceDirectory = dirname(fileURLToPath(import.meta.url));
const dashboardSource = readFileSync(join(sourceDirectory, 'dashboard.ts'), 'utf8');
const dashboardStyles = readFileSync(join(sourceDirectory, 'dashboard.css'), 'utf8');

const tickets = [
  { id: 'TK-SUBMITTED', status: 'submitted', review_offer: false, review_lease: false },
  { id: 'TK-OFFERED', status: 'submitted', review_offer: true, review_lease: false },
  { id: 'TK-ACTIVE', status: 'submitted', review_offer: false, review_lease: true },
  { id: 'TK-REVIEWING', status: 'reviewing', review_offer: false, review_lease: false },
  { id: 'TK-IN-REVIEW', status: 'in_review', review_offer: false, review_lease: false },
];

test('synthetic tickets separate submitted from every in-review state', () => {
  assert.deepEqual(tickets.map(ticketWorkStage), [
    'submitted', 'in_review', 'in_review', 'in_review', 'in_review',
  ]);
  assert.deepEqual(tickets.map(ticketReviewLabel), [
    null, 'Review offered', 'Review active', 'In review', 'In review',
  ]);
});

test('work counts move submitted review offers and leases into in review', () => {
  assert.deepEqual(workCounts(tickets, { submitted: 3, reviewing: 1, in_review: 1 }), {
    open: 0,
    working: 0,
    submitted: 1,
    inReview: 4,
    attention: 0,
  });
});

test('coordination NOW uses the actual work or review lease holder', () => {
  assert.equal(ticketNow({
    status: 'claimed',
    assigned_to: 'assigned-worker',
    claimed_by: 'active-worker',
    review_offer: false,
    review_lease: false,
  }, '9m left'), 'active-worker is working · 9m left');
  assert.equal(ticketNow({
    status: 'submitted',
    assigned_to: 'original-worker',
    reviewer_name: 'active-reviewer',
    reviewer_agent_id: 'AI-REVIEWER',
    review_offer: false,
    review_lease: true,
  }, undefined, '12m left'), 'active-reviewer is reviewing · 12m left');
  assert.equal(ticketNow({
    status: 'submitted',
    assigned_to: 'original-worker',
    review_offer: true,
    review_lease: false,
  }), 'Review offered · Not supplied');
});

test('coordination BLOCKED selects the newest decision or blocker deterministically', () => {
  const ticket = {
    status: 'claimed',
    review_offer: false,
    review_lease: false,
    annotations: [
      { id: 'AN-0001', kind: 'blocker', text: 'Old blocker', author: 'worker-a', at: '2030-01-01T00:00:00Z' },
      { id: 'AN-0003', kind: 'decision', text: 'Use the amended contract', author: 'coordinator-a', at: '2030-01-02T00:00:00Z' },
      { id: 'AN-0002', kind: 'decision', text: 'Superseded decision', author: 'coordinator-a', at: '2030-01-02T00:00:00Z' },
      { id: 'AN-0004', kind: 'evidence', text: 'Newer but not coordination', author: 'worker-a', at: '2030-01-03T00:00:00Z' },
    ],
  };
  assert.equal(
    ticketBlocker(ticket, [{ id: 'EV-9', seq: 9, kind: 'ticket_blocked', text: 'Event fallback' }]),
    'Decision: Use the amended contract · coordinator-a',
  );
  assert.equal(ticketBlocker({ ...ticket, annotations: [] }, []), 'None recorded');
});

test('ticket lifecycle and coordination source preserve truthful empty states', () => {
  assert.match(dashboardSource, /const LIFECYCLE_STAGES:[\s\S]*Open[\s\S]*Offered[\s\S]*Working[\s\S]*Submitted[\s\S]*Review[\s\S]*Resolved/);
  assert.match(dashboardSource, /observedAt \? formatTime\(observedAt\) : "Not observed"/);
  assert.match(dashboardSource, /\["Now", ticketNow\(\s*ticket,[\s\S]*\["Next",[\s\S]*\["Blocked", ticketBlocker\(ticket, ticketEvents\(data, ticket\.id\)\)\]/);
});

test('ticket activity uses the product event projection and a native disclosure', () => {
  assert.match(dashboardSource, /data\.events\.filter\(\(event\) => event\.ticket_id === ticketId\)/);
  assert.match(dashboardSource, /const visible = events\.slice\(0, 3\)/);
  assert.match(dashboardSource, /element\("details", "ticket-activity"\)/);
  assert.match(dashboardSource, /element\("summary", "ticket-activity-summary"\)/);
  assert.match(dashboardSource, /row\.setAttribute\("role", "listitem"\)/);
});

test('wide ticket layout keeps lifecycle and summary in readable rows', () => {
  assert.match(dashboardStyles, /\.lifecycle-rail\s*\{[\s\S]*grid-template-columns: repeat\(6, minmax\(0, 1fr\)\)/);
  assert.match(dashboardStyles, /\.ticket-summary\s*\{[\s\S]*grid-template-columns: repeat\(3, minmax\(0, 1fr\)\)/);
});

test('400px ticket layout stacks in DOM order without a second data model', () => {
  assert.match(dashboardSource, /matchMedia\("\(max-width: 620px\)"\)/);
  assert.match(dashboardStyles, /@media \(max-width: 620px\)[\s\S]*\.lifecycle-rail \{ grid-template-columns: 1fr;[\s\S]*\.ticket-summary \{ grid-template-columns: 1fr;/);
  assert.match(dashboardStyles, /overflow-wrap: anywhere/);
});
