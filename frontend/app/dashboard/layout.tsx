// Post-auth dashboard shell (Task 1): nav, tenant name, sign-out — plus
// the subscription gate. This calls the real GET /tenants/me backend
// route (app/api/tenants.py's get_my_tenant, behind get_current_tenant)
// and redirects on anything other than 'active' — it does NOT
// reimplement the active/pending check itself. The actual enforcement
// still lives in the backend: every real product route depends on
// get_active_tenant_id (app/core/deps.py), which 402s regardless of
// whether this redirect ever ran. This is the UX half only.
import Link from "next/link";
import { redirect } from "next/navigation";
import { OrganizationSwitcher, UserButton } from "@clerk/nextjs";
import { backendFetch } from "@/lib/api-server";

async function getMyTenant() {
  const resp = await backendFetch("/tenants/me");
  if (resp === null) redirect("/sign-in");
  if (resp.status === 403 || resp.status === 404) {
    // 403: signed in but no organization active in this session. 404: an
    // org is active but no tenants row exists for it yet — normally
    // momentary (organization.created's webhook hasn't landed yet right
    // after /create-organization). Either way, "/" has both the
    // OrganizationSwitcher and a path to create one.
    redirect("/");
  }
  if (!resp.ok) {
    throw new Error(`Could not load tenant (${resp.status})`);
  }
  return resp.json() as Promise<{ name: string; subscription_status: string }>;
}

// Rendered twice (once per breakpoint's <nav> in the header below,
// exactly one of the two ever visible at a time) so the link list itself
// — the part that actually changes when a feature gets added — is
// written once.
function NavLinks() {
  return (
    <>
      <Link href="/dashboard/sermons" className="hover:text-foreground text-muted-foreground whitespace-nowrap">
        Sermons
      </Link>
      {/* Its own nav entry, sibling to Sermons — a recording can exist
          before it's linked to any sermon (media_files.sermon_id is
          nullable), so this isn't nested under Sermons either. */}
      <Link href="/dashboard/recordings" className="hover:text-foreground text-muted-foreground whitespace-nowrap">
        Recordings
      </Link>
      {/* Phase 4 Task 3: its own nav entry, sibling to Sermons — not
          nested inside sermon generation. */}
      <Link href="/dashboard/study" className="hover:text-foreground text-muted-foreground whitespace-nowrap">
        Study
      </Link>
      {/* Same pattern — Compare and Concordance are their own standalone
          features, not nested under Study or Sermons. */}
      <Link href="/dashboard/compare" className="hover:text-foreground text-muted-foreground whitespace-nowrap">
        Compare
      </Link>
      <Link href="/dashboard/concordance" className="hover:text-foreground text-muted-foreground whitespace-nowrap">
        Concordance
      </Link>
    </>
  );
}

export default async function DashboardLayout({ children }: { children: React.ReactNode }) {
  const tenant = await getMyTenant();
  if (tenant.subscription_status !== "active") {
    redirect("/subscription-required");
  }

  return (
    <div className="flex min-h-screen flex-col">
      {/* Two-row on mobile (brand+account row, then a scrollable nav
          strip below), one row on sm+ — a phone-width OrganizationSwitcher
          already showing the org's name next to its icon left no room
          next to it for a single shared nav row; a shrink-0 nav squeezed
          off-screen there instead of actually wrapping into view. Two
          separate <nav> elements (one per breakpoint, only one ever
          rendered visible) rather than one nav that changes position,
          so nothing here has to fight CSS order for a reliable result —
          OrganizationSwitcher/UserButton still render exactly once. */}
      <header className="border-border border-b px-4 py-3">
        <div className="flex items-center justify-between gap-3">
          <div className="flex min-w-0 items-center gap-4">
            <Link href="/dashboard" className="shrink-0 text-sm font-semibold tracking-tight">
              Kerygma
            </Link>
            <span className="text-muted-foreground hidden shrink-0 truncate text-sm sm:inline">{tenant.name}</span>
            <nav className="hidden shrink-0 items-center gap-3 text-sm sm:flex">
              <NavLinks />
            </nav>
          </div>
          <div className="flex shrink-0 items-center gap-3">
            <OrganizationSwitcher hidePersonal createOrganizationUrl="/create-organization" />
            <UserButton />
          </div>
        </div>
        <nav className="mt-2 flex items-center gap-4 overflow-x-auto text-sm sm:hidden">
          <NavLinks />
        </nav>
      </header>
      <main className="flex-1 p-4 sm:p-6">{children}</main>
    </div>
  );
}
