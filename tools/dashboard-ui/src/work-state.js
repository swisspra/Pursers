const STATUS_STAGES = new Map([
  ['open', 'open'],
  ['assigned', 'open'],
  ['claimed', 'working'],
  ['in_progress', 'working'],
  ['creating_report', 'working'],
  ['reviewing', 'in_review'],
  ['in_review', 'in_review'],
  ['submitted', 'submitted'],
  ['rejected', 'attention'],
  ['closed', 'done'],
  ['canceled', 'ended'],
  ['terminated', 'ended'],
]);

function hasReviewActivity(ticket) {
  return ticket.review_lease === true || ticket.review_offer === true;
}

export function ticketWorkStage(ticket) {
  if (ticket.status === 'submitted' && hasReviewActivity(ticket)) return 'in_review';
  return STATUS_STAGES.get(ticket.status) ?? 'other';
}

export function ticketReviewLabel(ticket) {
  if (ticket.review_lease === true) return 'Review active';
  if (ticket.review_offer === true) return 'Review offered';
  if (ticket.status === 'reviewing' || ticket.status === 'in_review') return 'In review';
  return null;
}

export function workCounts(tickets, statusCounts) {
  const countStatus = (status) => {
    const aggregate = statusCounts[status];
    if (typeof aggregate === 'number' && Number.isFinite(aggregate)) return Math.max(0, aggregate);
    return tickets.filter((ticket) => ticket.status === status).length;
  };
  const submittedWithReview = tickets.filter(
    (ticket) => ticket.status === 'submitted' && hasReviewActivity(ticket),
  ).length;
  return {
    open: countStatus('open') + countStatus('assigned'),
    working: countStatus('claimed') + countStatus('in_progress') + countStatus('creating_report'),
    submitted: Math.max(0, countStatus('submitted') - submittedWithReview),
    inReview: countStatus('reviewing') + countStatus('in_review') + submittedWithReview,
    attention: countStatus('rejected'),
  };
}
