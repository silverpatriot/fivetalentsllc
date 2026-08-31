// Stripe Checkout's success_url (see backend/app/api/billing.py). The
// tenant doesn't actually flip to 'active' here — that happens
// asynchronously via the checkout.session.completed webhook
// (app/api/webhooks_stripe.py), which can land a moment after the
// browser redirect does. This page just sends them on to the dashboard,
// whose own gate (app/dashboard/layout.tsx) is the real source of truth
// and will bounce back to /subscription-required if the webhook hasn't
// landed yet.
import Link from "next/link";
import { Button } from "@/components/ui/button";

export default function BillingSuccessPage() {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-4 p-8 text-center">
      <h1 className="text-2xl font-semibold tracking-tight">You&apos;re subscribed</h1>
      <p className="text-muted-foreground max-w-sm text-sm">
        Setting up your workspace — this usually takes just a few seconds.
      </p>
      <Link href="/dashboard">
        <Button>Go to dashboard</Button>
      </Link>
    </div>
  );
}
