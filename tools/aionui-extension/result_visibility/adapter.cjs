'use strict';

const SAFE_BOARD = /^[A-Za-z0-9._-]{1,80}$/;
const SAFE_CENTRAL = /^[A-Za-z0-9._-]{1,80}$/;
const SAFE_TICKET = /^[A-Za-z0-9._-]{1,120}$/;
const SAFE_BRANCH = /^[A-Za-z0-9._/-]{1,200}$/;
const FULL_SHA = /^[0-9a-f]{40}$/;
const RESULT_STATES = new Set(['missing', 'pending', 'approved', 'rejected', 'failed']);
const MAX_RESULTS = 100;
const MAX_FILES = 50;

function text(value, limit) {
  if (typeof value !== 'string') return null;
  const compact = value.replace(/\s+/g, ' ').trim();
  return compact ? compact.slice(0, limit) : null;
}

function safeRelative(value) {
  if (typeof value !== 'string' || !value || value.length > 240 || value.includes('\\') || value.includes('\0')) return null;
  if (value.startsWith('/') || value.split('/').some((part) => !part || part === '.' || part === '..')) return null;
  return value;
}

function safeBranch(value) {
  if (typeof value !== 'string' || !SAFE_BRANCH.test(value) || value.startsWith('/')) return null;
  return value.split('/').some((part) => !part || part === '.' || part === '..') ? null : value;
}

function normalizeTicket(ticket) {
  if (!ticket || typeof ticket !== 'object' || !SAFE_TICKET.test(ticket.id || '')) return null;
  const raw = ticket.result;
  if (!raw || typeof raw !== 'object' || !RESULT_STATES.has(raw.state)) return null;
  const rawFiles = Array.isArray(raw.files_changed) ? raw.files_changed : [];
  const files = [];
  for (const value of rawFiles) {
    const candidate = safeRelative(value);
    if (candidate && !files.includes(candidate)) files.push(candidate);
    if (files.length >= MAX_FILES) break;
  }
  const branch = safeBranch(raw.branch);
  const commit = typeof raw.commit === 'string' && FULL_SHA.test(raw.commit) ? raw.commit : null;
  const review = raw.review && typeof raw.review === 'object' ? raw.review : {};
  const verdict = ['approve', 'reject'].includes(review.verdict) ? review.verdict : null;
  return {
    ticket_id: ticket.id,
    title: text(ticket.title, 160) || '(untitled)',
    ticket_status: text(ticket.status, 64) || 'unknown',
    result_state: raw.state,
    submitted_at: text(raw.submitted_at, 40),
    updated_at: text(ticket.updated_at, 40),
    submission: {
      summary: text(raw.summary, 1000),
      branch: branch && commit ? branch : null,
      commit: branch && commit ? commit : null,
      files_changed: files,
      files_omitted: Number.isInteger(raw.files_omitted) && raw.files_omitted >= 0
        ? Math.min(raw.files_omitted, 10000)
        : 0,
    },
    review: {
      verdict,
      reviewer: verdict ? text(review.reviewer, 96) : null,
      reviewed_at: verdict ? text(review.reviewed_at, 40) : null,
      independent: verdict ? review.independent === true : false,
    },
  };
}

function createResultVisibility({ expectedBoard, expectedCentral, fetchBoard }) {
  if (!SAFE_BOARD.test(expectedBoard || '')) throw new Error('expectedBoard must be a safe board identifier');
  if (!SAFE_CENTRAL.test(expectedCentral || '')) throw new Error('expectedCentral must be a safe Central label');
  if (typeof fetchBoard !== 'function') throw new Error('fetchBoard is required');

  async function read({ ticketId = null, state = null } = {}) {
    if (ticketId !== null && !SAFE_TICKET.test(ticketId)) {
      return { ok: false, code: 'invalid_ticket_id', retryable: false };
    }
    if (state !== null && !RESULT_STATES.has(state)) {
      return { ok: false, code: 'invalid_result_state', retryable: false };
    }
    let raw;
    try {
      raw = await fetchBoard(expectedBoard, expectedCentral);
    } catch (_error) {
      return { ok: false, code: 'backend_unavailable', retryable: true };
    }
    if (
      !raw
      || typeof raw !== 'object'
      || raw.central !== expectedCentral
      || raw.board?.board_id !== expectedBoard
      || !Array.isArray(raw.tickets)
    ) {
      return { ok: false, code: 'invalid_backend_response', retryable: true };
    }
    const normalized = raw.tickets.map(normalizeTicket).filter(Boolean);
    if (ticketId !== null) {
      const result = normalized.find((item) => item.ticket_id === ticketId);
      return result
        ? { ok: true, central: expectedCentral, board: expectedBoard, result }
        : { ok: false, code: 'ticket_not_found', retryable: false };
    }
    const filtered = state === null
      ? normalized
      : normalized.filter((item) => item.result_state === state);
    const results = filtered.slice(0, MAX_RESULTS);
    return {
      ok: true,
      central: expectedCentral,
      board: expectedBoard,
      generated_at: text(raw.generated_at, 40),
      results,
      returned: results.length,
      total: filtered.length,
      omitted: Math.max(0, filtered.length - results.length),
      invalid_omitted: Math.max(0, raw.tickets.length - normalized.length),
      truncated: Boolean(raw.truncated) || filtered.length > results.length,
    };
  }

  return { read };
}

module.exports = {
  MAX_FILES,
  MAX_RESULTS,
  RESULT_STATES,
  createResultVisibility,
  normalizeTicket,
};
