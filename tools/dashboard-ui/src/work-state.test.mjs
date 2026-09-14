import assert from 'node:assert/strict';
import test from 'node:test';

import { ticketReviewLabel, ticketWorkStage, workCounts } from './work-state.js';

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
