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

function firstSupplied(...values) {
  return values.find((value) => typeof value === 'string' && value.length > 0) ?? 'Not supplied';
}

export function ticketNow(
  ticket,
  leaseLabel = 'lease Not observed',
  reviewLeaseLabel = 'lease Not observed',
) {
  const status = ticket.status.toLowerCase();
  const worker = firstSupplied(
    ticket.claimed_by,
    ticket.assigned_to,
    ticket.claimed_agent_id,
    ticket.assigned_agent_id,
  );
  const reviewer = ticket.review_lease === true
    ? firstSupplied(ticket.reviewer_name, ticket.reviewer_agent_id)
    : firstSupplied(ticket.review_offer_name, ticket.review_offer_agent_id);

  if (['claimed', 'in_progress', 'creating_report'].includes(status)) {
    return `${worker} is working · ${leaseLabel}`;
  }
  if (ticket.review_lease === true) return `${reviewer} is reviewing · ${reviewLeaseLabel}`;
  if (ticket.review_offer === true) return `Review offered · ${reviewer}`;
  if (status === 'submitted') return 'Submitted · reviewer Not supplied';
  if (status === 'rejected') return `Rejected · ${worker}`;
  if (['closed', 'canceled', 'terminated'].includes(status)) return status;
  if (['open', 'assigned'].includes(status)) return `Open · worker ${worker}`;
  return `${ticket.status || 'Not observed'} · owner ${worker}`;
}

function newestCoordinationAnnotation(annotations) {
  return [...(annotations ?? [])]
    .filter((annotation) => {
      const kind = String(annotation.kind ?? '').toLowerCase();
      return kind === 'decision' || kind === 'blocker' || kind === 'blocked';
    })
    .sort((left, right) => {
      const timeOrder = String(right.at ?? '').localeCompare(String(left.at ?? ''));
      if (timeOrder !== 0) return timeOrder;
      return String(right.id ?? '').localeCompare(String(left.id ?? ''));
    })[0] ?? null;
}

export function ticketBlocker(ticket, events) {
  const annotation = newestCoordinationAnnotation(ticket.annotations);
  if (annotation) {
    const label = String(annotation.kind).toLowerCase() === 'decision' ? 'Decision' : 'Blocker';
    const body = firstSupplied(annotation.text);
    const author = firstSupplied(annotation.author, annotation.author_agent_id);
    return `${label}: ${body} · ${author}`;
  }
  const blocker = [...events]
    .filter((event) => {
      const kind = event.kind.toLowerCase();
      return kind.includes('blocker') || kind.includes('blocked');
    })
    .sort((left, right) => {
      const seqOrder = (right.seq ?? -1) - (left.seq ?? -1);
      if (seqOrder !== 0) return seqOrder;
      const timeOrder = String(right.occurred_at ?? '').localeCompare(String(left.occurred_at ?? ''));
      if (timeOrder !== 0) return timeOrder;
      return right.id.localeCompare(left.id);
    })[0];
  if (blocker) return blocker.text;
  if (ticket.status.toLowerCase() === 'rejected' || ticket.rejected === true) {
    return 'Rejected by independent review';
  }
  const omitted = Number.isFinite(ticket.annotations_omitted_count)
    ? Math.max(0, ticket.annotations_omitted_count)
    : 0;
  if (omitted > 0) {
    return `Not observed · ${omitted} older annotation${omitted === 1 ? '' : 's'} omitted`;
  }
  return 'None recorded';
}
