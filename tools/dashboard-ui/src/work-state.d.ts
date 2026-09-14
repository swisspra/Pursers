export type ReviewAwareTicket = {
  status: string;
  review_offer?: boolean;
  review_lease?: boolean;
};

export type CoordinationAnnotation = {
  id?: string | null;
  kind?: string | null;
  text?: string | null;
  author?: string | null;
  author_agent_id?: string | null;
  at?: string | null;
};

export type CoordinationTicket = ReviewAwareTicket & {
  assigned_to?: string | null;
  assigned_agent_id?: string | null;
  claimed_by?: string | null;
  claimed_agent_id?: string | null;
  reviewer_name?: string | null;
  reviewer_agent_id?: string | null;
  review_offer_name?: string | null;
  review_offer_agent_id?: string | null;
  rejected?: boolean;
  annotations?: readonly CoordinationAnnotation[];
};

export type CoordinationEvent = {
  id: string;
  seq?: number | null;
  kind: string;
  text: string;
  occurred_at?: string | null;
};

export type WorkStage = 'open' | 'working' | 'submitted' | 'in_review' | 'attention' | 'done' | 'ended' | 'other';

export function ticketWorkStage(ticket: ReviewAwareTicket): WorkStage;
export function ticketReviewLabel(ticket: ReviewAwareTicket): string | null;
export function ticketNow(
  ticket: CoordinationTicket,
  leaseLabel?: string,
  reviewLeaseLabel?: string,
): string;
export function ticketBlocker(
  ticket: CoordinationTicket,
  events: readonly CoordinationEvent[],
): string;
export function workCounts(
  tickets: readonly ReviewAwareTicket[],
  statusCounts: Readonly<Record<string, number>>,
): { open: number; working: number; submitted: number; inReview: number; attention: number };
