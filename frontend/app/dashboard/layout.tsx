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

export default async function DashboardLayout({ children }: { children: React.ReactNode }) {
  const tenant = await getMyTenant();
  if (tenant.subscription_status !== "active") {
    redirect("/subscription-required");
  }

  return (
    <div className="flex min-h-screen flex-col">
      <header className="border-border flex items-center justify-between border-b px-4 py-3">
        <div className="flex items-center gap-4">
          <Link href="/dashboard" className="text-sm font-semibold tracking-tight">
            Kerygma
          </Link>
          <span className="text-muted-foreground text-sm">{tenant.name}</span>
          <nav className="flex items-center gap-3 text-sm">
            <Link href="/dashboard/sermons" className="hover:text-foreground text-muted-foreground">
              Sermons
            </Link>
            {/* Its own nav entry, sibling to Sermons — a recording can
                exist before it's linked to any sermon (media_files.sermon_id
                is nullable), so this isn't nested under Sermons either. */}
            <Link href="/dashboard/recordings" className="hover:text-foreground text-muted-foreground">
              Recordings
            </Link>
            {/* Phase 4 Task 3: its own nav entry, sibling to Sermons —
                not nested inside sermon generation. */}
            <Link href="/dashboard/study" className="hover:text-foreground text-muted-foreground">
              Study
            </Link>
            {/* Same pattern — Compare and Concordance are their own
                standalone features, not nested under Study or Sermons. */}
            <Link href="/dashboard/compare" className="hover:text-foreground text-muted-foreground">
              Compare
            </Link>
            <Link href="/dashboard/concordance" className="hover:text-foreground text-muted-foreground">
              Concordance
            </Link>
          </nav>
        </div>
        <div className="flex items-center gap-3">
          <OrganizationSwitcher hidePersonal createOrganizationUrl="/create-organization" />
          <UserButton />
        </div>
      </header>
      <main className="flex-1 p-6">{children}</main>
    </div>
  );
}
