import { Show, UserButton, OrganizationSwitcher } from "@clerk/nextjs";
import Link from "next/link";
import { buttonVariants } from "@/components/ui/button";
import { cn } from "@/lib/utils";

// <SignedIn>/<SignedOut> were removed in Clerk Core 3 (March 2026),
// replaced by this single <Show when="..."> component — checked against
// Clerk's current docs after the old components broke the build, not
// assumed from memory.
export default function Home() {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-4 p-8">
      <h1 className="text-3xl font-semibold tracking-tight">Kerygma</h1>
      <p className="text-muted-foreground max-w-md text-center text-sm">
        Phase 2 scaffold: auth (Clerk) and billing (Stripe) are wired.
        The actual product UI lands in a later phase.
      </p>

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
          <OrganizationSwitcher hidePersonal createOrganizationUrl="/create-organization" />
          <UserButton />
        </div>
      </Show>

      <a href="/api/health" className={cn(buttonVariants({ variant: "ghost" }))}>
        Check backend health
      </a>
    </div>
  );
}
