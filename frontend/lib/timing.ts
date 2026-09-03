// Phase 7 Task 2: delivery-time estimate. Pure client-side arithmetic —
// no backend call, so there's nothing to go stale: every render just
// recomputes off whatever content is currently in view.
//
// Mirrors backend/app/services/context_assembly.py's PREACHING_WORDS_PER_
// MINUTE exactly — that's the same pace generation itself targets to hit
// a "20-25 minute" sermon, so this estimate MUST use the identical
// number, not an independently-chosen one. Otherwise a sermon generated
// to hit "20-25 minutes" could report back a mismatched estimate here
// (e.g. 32 minutes), a visible inconsistency a pastor would notice
// immediately. If that backend constant ever changes, change this one to
// match.
export const PREACHING_WORDS_PER_MINUTE = 130;

/** Rounded minutes to deliver `content` aloud at the standard pace above.
 * Returns 0 for empty/whitespace-only content rather than NaN. */
export function estimateDeliveryMinutes(content: string): number {
  const words = content.trim().split(/\s+/).filter(Boolean).length;
  return Math.round(words / PREACHING_WORDS_PER_MINUTE);
}

/** "~28 min" — a single number, not a range, per the Phase 7 Task 2
 * design checkpoint: a range accounting for pace variation was
 * considered and deliberately not built — a simple estimate with an
 * explanatory note next to it (see callers) was judged sufficient. */
export function formatDeliveryEstimate(content: string): string {
  const minutes = estimateDeliveryMinutes(content);
  if (minutes <= 0) return "~0 min";
  return `~${minutes} min`;
}
