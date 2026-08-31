/**
 * Resolves which tenant a request is for, from its Host header — the
 * subdomain-routing piece for Task 1. `gracecommunity.kerygma.church`
 * (prod) or `gracecommunity.localhost:3000` (local dev, no /etc/hosts
 * edits needed — most OSes/browsers resolve any `*.localhost` to
 * 127.0.0.1 out of the box) both resolve to slug "gracecommunity". The
 * bare base domain, `www`, and plain `localhost` resolve to no tenant at
 * all — that's the marketing/landing-page case, not an error.
 *
 * This is UX routing only (which branding to show, which org to
 * auto-select) — it is never the access-control decision. The backend
 * derives tenant_id strictly from the verified Clerk session's org claim
 * (see backend/app/core/security.py's extract_org_context), completely
 * independent of whatever hostname the request arrived on.
 */
export function getTenantSlugFromHost(host: string | null | undefined): string | null {
  if (!host) return null;
  const hostname = host.split(":")[0].toLowerCase();
  const baseDomain = (process.env.NEXT_PUBLIC_APP_BASE_DOMAIN || "kerygma.church").toLowerCase();

  if (hostname === baseDomain || hostname === `www.${baseDomain}`) return null;
  if (hostname === "localhost" || hostname === "127.0.0.1") return null;

  if (hostname.endsWith(`.${baseDomain}`)) {
    const sub = hostname.slice(0, -(`.${baseDomain}`.length));
    return sub && sub !== "www" ? sub : null;
  }

  if (hostname.endsWith(".localhost")) {
    const sub = hostname.slice(0, -".localhost".length);
    return sub || null;
  }

  return null;
}
