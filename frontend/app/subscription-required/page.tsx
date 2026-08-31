// Where app/dashboard/layout.tsx redirects a signed-in tenant whose
// subscription_status isn't 'active' — the UX half of Task 1's gating
// requirement. Reachable without an active subscription on purpose (see
// middleware.ts's public-route list); the actual enforcement is
// get_active_tenant_id in the backend, not this page.
import { PricingTiers } from "@/components/pricing-tiers";

export default function SubscriptionRequiredPage() {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-6 p-8">
      <div className="max-w-md text-center">
        <h1 className="text-2xl font-semibold tracking-tight">Subscription required</h1>
        <p className="text-muted-foreground mt-2 text-sm">
          Your church&apos;s workspace doesn&apos;t have an active subscription yet. Choose a plan
          below to get started.
        </p>
      </div>
      <PricingTiers />
    </div>
  );
}
