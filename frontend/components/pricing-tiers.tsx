"use client";

import { useState } from "react";
import { useOrganization, Show } from "@clerk/nextjs";
import Link from "next/link";
import { Check } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

// $29/$79 are a real pricing decision (Phase 5), not the original
// Phase 2 $49/$149/$399 placeholders — see scripts/stripe_setup.py's
// module docstring for the full history. Still not validated against
// real usage/margin data, so treat as provisional. Enterprise stays an
// internal reference number only (contact-sales, no self-serve
// Checkout button below) and Free needs no dollar figure to track at
// all — see app/api/billing.py's activate_free_tier.
//
// Quotas are app/services/plan_limits.py's PLAN_TIER_MONTHLY_AI_
// GENERATIONS, kept in sync here by hand for now: one full sermon
// (outline + draft) uses 2 of these, so "~N sermons" below is quota/2,
// rounded down — regenerating a preaching outline afterward uses a
// third.
const TIERS = [
  {
    planTier: "free",
    kind: "free" as const,
    name: "Free",
    price: "$0",
    blurb: "Try it with your own church, no card required.",
    features: ["8 AI generations/mo (~4 sermons)", "Scripture-verified citations", "Compare & concordance tools"],
    highlighted: false,
  },
  {
    planTier: "starter",
    kind: "paid" as const,
    name: "Starter",
    price: "$29",
    blurb: "One church, preaching weekly.",
    features: ["40 AI generations/mo (~20 sermons)", "Everything in Free", "Matches your own past sermons' voice", "Email support"],
    highlighted: false,
  },
  {
    planTier: "growth",
    kind: "paid" as const,
    name: "Growth",
    price: "$79",
    blurb: "Multiple campuses or a full media team.",
    features: ["150 AI generations/mo (~75 sermons)", "Everything in Starter", "Multiple campus/team accounts", "Priority support"],
    highlighted: true,
  },
  {
    planTier: "enterprise",
    kind: "contact" as const,
    name: "Enterprise",
    price: "Custom",
    blurb: "Denominational networks and large multi-site churches.",
    features: ["Unlimited AI generations", "Everything in Growth", "Custom onboarding", "Dedicated support"],
    highlighted: false,
  },
] as const;

// Placeholder inbox — swap for a real one before launch (same
// not-a-real-decision-yet flag as the dollar amounts above).
const SALES_EMAIL = "sales@kerygma.church";

function SubscribeButton({ planTier, highlighted }: { planTier: string; highlighted?: boolean }) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { organization } = useOrganization();

  async function startCheckout() {
    setLoading(true);
    setError(null);
    try {
      const resp = await fetch("/api/billing/checkout", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ plan_tier: planTier }),
      });
      if (resp.status === 404) {
        // The only way POST /billing/checkout 404s is get_current_tenant
        // (app/core/deps.py) finding no tenants row for this org yet —
        // normal right after creating an organization, before Clerk's
        // organization.created webhook has been processed (or, if it
        // persists, a sign that webhook delivery isn't configured/
        // working at all). Either way, the raw backend detail ("Unknown
        // tenant") is an implementation detail, not something to show a
        // pastor as-is.
        throw new Error(
          "Your church's workspace is still being set up — this can take a few seconds after creating your organization. Try again shortly."
        );
      }
      const data = await resp.json();
      if (!resp.ok) {
        throw new Error(data.detail || "Could not start checkout");
      }
      window.location.href = data.checkout_url;
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not start checkout");
      setLoading(false);
    }
  }

  return (
    <div className="flex flex-col gap-1.5">
      <Show when="signed-out">
        <Link href="/sign-up" className={cn(buttonClass(highlighted), "w-full")}>
          Get started
        </Link>
      </Show>
      <Show when="signed-in">
        {organization ? (
          <Button className="w-full" variant={highlighted ? "default" : "outline"} onClick={startCheckout} disabled={loading}>
            {loading ? "Redirecting…" : "Subscribe"}
          </Button>
        ) : (
          <Link href="/create-organization" className={cn(buttonClass(highlighted), "w-full")}>
            Create your church&apos;s workspace
          </Link>
        )}
      </Show>
      {error && (
        <p className="bg-destructive/10 text-destructive rounded-md px-2 py-1.5 text-xs">{error}</p>
      )}
    </div>
  );
}

function FreeTierButton() {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { organization } = useOrganization();

  async function activateFree() {
    setLoading(true);
    setError(null);
    try {
      const resp = await fetch("/api/billing/activate-free", { method: "POST" });
      const data = await resp.json();
      if (!resp.ok) {
        // activate_free_tier's one real failure case: an already-active
        // (usually paid) tenant hitting this again — that's not
        // something a "Get started free" click should ever normally
        // trigger, but if it does, send them to the dashboard rather
        // than showing a confusing error about a plan they already have.
        window.location.href = "/dashboard";
        return;
      }
      window.location.href = "/dashboard";
    } catch {
      setError("Could not activate the free plan. Please try again.");
      setLoading(false);
    }
  }

  return (
    <div className="flex flex-col gap-1.5">
      <Show when="signed-out">
        <Link href="/sign-up" className={cn(buttonClass(false), "w-full")}>
          Get started free
        </Link>
      </Show>
      <Show when="signed-in">
        {organization ? (
          <Button className="w-full" variant="outline" onClick={activateFree} disabled={loading}>
            {loading ? "Setting up…" : "Get started free"}
          </Button>
        ) : (
          <Link href="/create-organization" className={cn(buttonClass(false), "w-full")}>
            Create your church&apos;s workspace
          </Link>
        )}
      </Show>
      {error && (
        <p className="bg-destructive/10 text-destructive rounded-md px-2 py-1.5 text-xs">{error}</p>
      )}
    </div>
  );
}

function buttonClass(highlighted?: boolean) {
  return cn(
    "inline-flex h-8 items-center justify-center rounded-lg border border-transparent px-2.5 text-sm font-medium transition-all",
    highlighted ? "bg-primary text-primary-foreground hover:bg-primary/80" : "border-border bg-background hover:bg-muted"
  );
}

export function PricingTiers() {
  return (
    <div className="grid w-full max-w-5xl grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
      {TIERS.map((tier) => (
        <div
          key={tier.planTier}
          className={cn(
            "flex flex-col gap-3 rounded-xl border p-5",
            tier.highlighted ? "border-primary shadow-sm" : "border-border"
          )}
        >
          <div>
            <h3 className="text-sm font-semibold">{tier.name}</h3>
            <p className="mt-1 text-2xl font-semibold tracking-tight">
              {tier.price}
              {tier.kind !== "contact" && <span className="text-muted-foreground text-sm font-normal">/mo</span>}
            </p>
            <p className="text-muted-foreground mt-1 text-xs">{tier.blurb}</p>
          </div>
          <ul className="text-muted-foreground flex flex-1 flex-col gap-1.5 text-xs">
            {tier.features.map((f) => (
              <li key={f} className="flex items-start gap-1.5">
                <Check className="text-primary mt-0.5 size-3.5 shrink-0" />
                {f}
              </li>
            ))}
          </ul>
          {tier.kind === "free" && <FreeTierButton />}
          {tier.kind === "paid" && <SubscribeButton planTier={tier.planTier} highlighted={tier.highlighted} />}
          {tier.kind === "contact" && (
            <a href={`mailto:${SALES_EMAIL}?subject=Kerygma Enterprise`} className={cn(buttonClass(false), "w-full")}>
              Contact us
            </a>
          )}
        </div>
      ))}
    </div>
  );
}
