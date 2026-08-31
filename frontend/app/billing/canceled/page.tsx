// Stripe Checkout's cancel_url (see backend/app/api/billing.py).
import Link from "next/link";
import { Button } from "@/components/ui/button";

export default function BillingCanceledPage() {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-4 p-8 text-center">
      <h1 className="text-2xl font-semibold tracking-tight">Checkout canceled</h1>
      <p className="text-muted-foreground max-w-sm text-sm">
        No charge was made. You can pick a plan again whenever you&apos;re ready.
      </p>
      <Link href="/">
        <Button variant="outline">Back to Kerygma</Button>
      </Link>
    </div>
  );
}
