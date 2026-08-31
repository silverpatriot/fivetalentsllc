// Server-side-only helpers for calling the backend. The backend is
// reachable only on the internal Docker network (see docker-compose.yml
// and README's "Webhooks are proxied through Next.js" section) — nothing
// here is ever safe to call from a Client Component; browser code goes
// through the app/api/* route handlers instead, which forward to these
// same backend routes over the server-side network.
import "server-only";
import { auth } from "@clerk/nextjs/server";

const BACKEND_URL = process.env.BACKEND_INTERNAL_URL || "http://backend:8000";

/** Authenticated backend call: attaches the current Clerk session's
 * bearer token, same as every other tenant-scoped backend route expects.
 * Returns null (not a thrown error) if there's no signed-in session —
 * callers decide what "not signed in" means for their own page. */
export async function backendFetch(path: string, init: RequestInit = {}): Promise<Response | null> {
  const { getToken } = await auth();
  const token = await getToken();
  if (!token) return null;

  const headers = new Headers(init.headers);
  headers.set("Authorization", `Bearer ${token}`);
  // NOT for a FormData body (Phase 4 Task 3's document upload proxy) —
  // fetch computes the correct `multipart/form-data; boundary=...` value
  // itself only when Content-Type is left unset; forcing it to
  // application/json here would silently corrupt every file upload.
  if (init.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  return fetch(`${BACKEND_URL}${path}`, { ...init, headers, cache: "no-store" });
}

/** Unauthenticated backend call — only for routes that are deliberately
 * public (currently just GET /tenants/by-slug/{slug}, for pre-auth
 * subdomain branding). Never use this for anything tenant-data-bearing. */
export async function backendFetchPublic(path: string, init: RequestInit = {}): Promise<Response> {
  return fetch(`${BACKEND_URL}${path}`, { ...init, cache: "no-store" });
}
