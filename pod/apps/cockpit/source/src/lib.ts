// Small client-side helpers for the cockpit. Formatting + SDK-shape unwrapping
// only — no business logic (the cockpit is a read surface; all logic lives in
// alpha-core / the pod functions).

/** Unwrap a `{ items: T[] }` SDK list response to its array (or []). */
export function getItems<T>(value: unknown): T[] {
  if (
    value &&
    typeof value === 'object' &&
    'items' in value &&
    Array.isArray((value as { items: unknown }).items)
  ) {
    return (value as { items: T[] }).items
  }
  return []
}

/** A human message for any thrown/SDK error value. */
export function errMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}
