export type ReviewAwareTicket = {
  status: string;
  review_offer?: boolean;
  review_lease?: boolean;
};

export type WorkStage = 'open' | 'working' | 'submitted' | 'in_review' | 'attention' | 'done' | 'ended' | 'other';

export function ticketWorkStage(ticket: ReviewAwareTicket): WorkStage;
export function ticketReviewLabel(ticket: ReviewAwareTicket): string | null;
export function workCounts(
  tickets: readonly ReviewAwareTicket[],
  statusCounts: Readonly<Record<string, number>>,
): { open: number; working: number; submitted: number; inReview: number; attention: number };
