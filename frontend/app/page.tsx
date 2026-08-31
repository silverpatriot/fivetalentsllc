import { Show, UserButton, OrganizationSwitcher } from "@clerk/nextjs";
import Link from "next/link";
import { buttonVariants } from "@/components/ui/button";
import { PricingTiers } from "@/components/pricing-tiers";
import { cn } from "@/lib/utils";

// <SignedIn>/<SignedOut> were removed in Clerk Core 3 (March 2026),
// replaced by this single <Show when="..."> component — checked against
// Clerk's current docs after the old components broke the build, not
// assumed from memory.
export default function Home() {
  return (
    <div className="flex min-h-screen flex-col items-center gap-16 px-6 py-16">
      <header className="flex w-full max-w-4xl items-center justify-between">
        <span className="text-lg font-semibold tracking-tight">Kerygma</span>
        <Show when="signed-out">
          <div className="flex gap-2">
            <Link href="/sign-in" className={cn(buttonVariants({ variant: "outline" }))}>
              Sign in
            </Link>
            <Link href="/sign-up" className={cn(buttonVariants())}>
              Sign up
            </Link>
          </div>
        </Show>
        <Show when="signed-in">
          <div className="flex items-center gap-3">
            <Link href="/dashboard" className={cn(buttonVariants({ variant: "outline" }))}>
              Dashboard
            </Link>
            <OrganizationSwitcher hidePersonal createOrganizationUrl="/create-organization" />
            <UserButton />
          </div>
        </Show>
      </header>

      <div className="flex flex-col items-center gap-4 text-center">
        <h1 className="max-w-xl text-4xl font-semibold tracking-tight text-balance">
          Sermon preparation, in your own voice
        </h1>
        <p className="text-muted-foreground max-w-md text-sm text-balance">
          AI-assisted sermon drafting with verified scripture citations — for pastors, not
          against them.
        </p>
      </div>

      <PricingTiers />
    </div>
  );
}
