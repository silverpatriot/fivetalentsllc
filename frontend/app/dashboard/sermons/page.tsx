import Link from "next/link";
import { redirect } from "next/navigation";
import { Button } from "@/components/ui/button";
import { backendFetch } from "@/lib/api-server";

type Sermon = {
  id: string;
  title: string;
  format: string;
  status: string;
  created_at: string;
};

async function getSermons(): Promise<Sermon[]> {
  const resp = await backendFetch("/sermons");
  if (resp === null) redirect("/sign-in");
  if (!resp.ok) throw new Error(`Could not load sermons (${resp.status})`);
  return resp.json();
}

const STATUS_LABEL: Record<string, string> = {
  draft: "Draft",
  generating: "Generating…",
  // 2026-09-04: an attempt was cut short by a disconnect mid-stream, not
  // a failure and not "never attempted" — see app/services/generation.py
  // (backend)'s `_run`, `except (GeneratorExit, asyncio.CancelledError)`.
  interrupted: "Interrupted — retry",
  ready: "Ready for review",
  published: "Published",
};

export default async function SermonsListPage() {
  const sermons = await getSermons();

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold tracking-tight">Sermons</h1>
        <Link href="/dashboard/sermons/new">
          <Button>New sermon</Button>
        </Link>
      </div>

      {sermons.length === 0 ? (
        <p className="text-muted-foreground text-sm">No sermons yet — create your first one.</p>
      ) : (
        <ul className="divide-border divide-y rounded-lg border">
          {sermons.map((s) => (
            <li key={s.id}>
              <Link
                href={`/dashboard/sermons/${s.id}`}
                className="hover:bg-muted flex items-center justify-between px-4 py-3 text-sm"
              >
                <span className="font-medium">{s.title}</span>
                <span className="text-muted-foreground flex items-center gap-3 text-xs">
                  <span className="capitalize">{s.format}</span>
                  <span>{STATUS_LABEL[s.status] ?? s.status}</span>
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
