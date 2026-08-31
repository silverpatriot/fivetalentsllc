"use client";

import { useState } from "react";
import { useOrganization, Show } from "@clerk/nextjs";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

// $49/$149/$399 are explicit placeholders (see README's Billing section)
// carried over from Phase 2's stripe_setup.py bootstrap, not a real
// pricing decision — kept in sync with that script's PLAN_TIERS by hand
// for now.
const TIERS = [
  {
    planTier: "starter",
    name: "Starter",
    price: "$49",
    blurb: "One church, getting started with AI-assisted sermon prep.",
    features: ["Unlimited sermon drafts", "Scripture-verified citations", "Email support"],
    highlighted: false,
  },
  {
    planTier: "growth",
    name: "Growth",
    price: "$149",
    blurb: "Multiple campuses or a full media team.",
    features: ["Everything in Starter", "Transcription & clip generation", "Priority support"],
    highlighted: true,
  },
  {
    planTier: "enterprise",
    name: "Enterprise",
    price: "$399",
    blurb: "Denominational networks and large multi-site churches.",
    features: ["Everything in Growth", "Custom onboarding", "Dedicated support"],
    highlighted: false,
  },
] as const;

function CheckoutButton({ planTier, highlighted }: { planTier: string; highlighted?: boolean }) {
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
      {error && <p className="text-destructive text-xs">{error}</p>}
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
    <div className="grid w-full max-w-4xl grid-cols-1 gap-4 sm:grid-cols-3">
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
              <span className="text-muted-foreground text-sm font-normal">/mo</span>
            </p>
            <p className="text-muted-foreground mt-1 text-xs">{tier.blurb}</p>
          </div>
          <ul className="text-muted-foreground flex flex-1 flex-col gap-1 text-xs">
            {tier.features.map((f) => (
              <li key={f}>· {f}</li>
            ))}
          </ul>
          <CheckoutButton planTier={tier.planTier} highlighted={tier.highlighted} />
        </div>
      ))}
    </div>
  );
}
