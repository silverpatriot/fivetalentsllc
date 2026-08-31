// Protects every route except the ones explicitly listed as public.
// Actual data access is still gated server-side (get_db / get_active_tenant_id
// in the backend) regardless of what this middleware does — this is UX
// (redirect to sign-in), not the security boundary.
import { clerkMiddleware, createRouteMatcher } from "@clerk/nextjs/server";
import { NextResponse } from "next/server";
import { getTenantSlugFromHost } from "@/lib/tenant";

const isPublicRoute = createRouteMatcher([
  "/",
  "/sign-in(.*)",
  "/sign-up(.*)",
  "/subscription-required",
  "/api/health",
  "/api/webhooks/(.*)", // Stripe/Clerk calling us — never a signed-in browser session
]);

export default clerkMiddleware(async (auth, req) => {
  if (!isPublicRoute(req)) {
    await auth.protect();
  }

  // Subdomain routing (Task 1): resolve which tenant this request is for
  // from its Host header, and hand that down to server components via a
  // request header — next/headers' headers() reads it as
  // "x-tenant-slug". This is branding/UX only (see lib/tenant.ts's
  // docstring) — never trusted as an access-control decision anywhere
  // downstream.
  const tenantSlug = getTenantSlugFromHost(req.headers.get("host"));
  const requestHeaders = new Headers(req.headers);
  if (tenantSlug) {
    requestHeaders.set("x-tenant-slug", tenantSlug);
  } else {
    requestHeaders.delete("x-tenant-slug"); // never trust a client-supplied copy of this header
  }
  return NextResponse.next({ request: { headers: requestHeaders } });
});

export const config = {
  matcher: [
    "/((?!_next|[^?]*\\.(?:html?|css|js(?!on)|jpe?g|webp|png|gif|svg|ttf|woff2?|ico|csv|docx?|xlsx?|zip|webmanifest)).*)",
    "/(api|trpc)(.*)",
  ],
};
