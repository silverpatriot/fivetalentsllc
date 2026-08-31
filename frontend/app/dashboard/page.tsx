import Link from "next/link";
import { Button } from "@/components/ui/button";

export default function DashboardHome() {
  return (
    <div className="mx-auto flex max-w-2xl flex-col items-start gap-4">
      <h1 className="text-xl font-semibold tracking-tight">Dashboard</h1>
      <p className="text-muted-foreground text-sm">
        Start a new sermon, or pick up a draft you already started.
      </p>
      <div className="flex gap-2">
        <Link href="/dashboard/sermons/new">
          <Button>New sermon</Button>
        </Link>
        <Link href="/dashboard/sermons">
          <Button variant="outline">View all sermons</Button>
        </Link>
      </div>
    </div>
  );
}
