import { buttonVariants } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export default function Home() {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-4 p-8">
      <h1 className="text-3xl font-semibold tracking-tight">Sermon Engine</h1>
      <p className="text-muted-foreground max-w-md text-center text-sm">
        Phase 1 scaffold. Auth (Clerk), billing (Stripe), and the actual
        product UI land in later tasks.
      </p>
      <a href="/api/health" className={cn(buttonVariants())}>
        Check backend health
      </a>
    </div>
  );
}
