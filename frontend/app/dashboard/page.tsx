import Link from "next/link";
import { BookOpen, FileText, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { backendFetch } from "@/lib/api-server";

type Sermon = {
  id: string;
  title: string;
  format: string;
  status: string;
  created_at: string;
};

type StudyDocument = { id: string };

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

// Real first-run/home content, not just a nav stub — pulled from the
// same GET /sermons and GET /documents backendFetch calls sermons/page.tsx
// and study/page.tsx already make, so no new backend route is needed
// for this reshape.
async function getRecentSermons(): Promise<Sermon[]> {
  const resp = await backendFetch("/sermons");
  if (resp === null || !resp.ok) return [];
  return resp.json();
}

async function getStudyDocumentCount(): Promise<number> {
  const resp = await backendFetch("/documents?corpus_type=theology");
  if (resp === null || !resp.ok) return 0;
  const docs: StudyDocument[] = await resp.json();
  return docs.length;
}

export default async function DashboardHome() {
  const [sermons, studyDocumentCount] = await Promise.all([getRecentSermons(), getStudyDocumentCount()]);
  const inProgressCount = sermons.filter(
    (s) => s.status === "draft" || s.status === "generating" || s.status === "interrupted",
  ).length;
  const recent = sermons.slice(0, 3);

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-6">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Dashboard</h1>
        <p className="text-muted-foreground mt-1 text-sm">
          Start a new sermon, pick up a draft, or study a passage.
        </p>
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <Card>
          <CardHeader className="flex-row items-center gap-2 space-y-0">
            <FileText className="text-primary size-4" />
            <CardTitle>Sermons</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <p className="text-2xl font-semibold tracking-tight">
              {sermons.length}
              {inProgressCount > 0 && (
                <span className="text-muted-foreground ml-1.5 text-xs font-normal">
                  · {inProgressCount} in progress
                </span>
              )}
            </p>
            <div className="flex gap-2">
              <Link href="/dashboard/sermons/new">
                <Button size="sm">
                  <Plus /> New sermon
                </Button>
              </Link>
              <Link href="/dashboard/sermons">
                <Button size="sm" variant="outline">
                  View all
                </Button>
              </Link>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex-row items-center gap-2 space-y-0">
            <BookOpen className="text-primary size-4" />
            <CardTitle>Study</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <p className="text-2xl font-semibold tracking-tight">
              {studyDocumentCount}
              <span className="text-muted-foreground ml-1.5 text-xs font-normal">documents uploaded</span>
            </p>
            <Link href="/dashboard/study">
              <Button size="sm" variant="outline">
                Open Study
              </Button>
            </Link>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="space-y-0">
            <CardTitle>Quick actions</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-2">
            <Link href="/dashboard/sermons/new" className="text-sm hover:underline">
              New sermon
            </Link>
            <Link href="/dashboard/study" className="text-sm hover:underline">
              Ask a study question
            </Link>
            <Link href="/dashboard/compare" className="text-sm hover:underline">
              Compare translations
            </Link>
            <Link href="/dashboard/concordance" className="text-sm hover:underline">
              Search the concordance
            </Link>
          </CardContent>
        </Card>
      </div>

      <div>
        <h2 className="mb-2 text-sm font-semibold">Recent sermons</h2>
        {recent.length === 0 ? (
          <Card>
            <CardContent className="text-muted-foreground py-6 text-center text-sm">
              No sermons yet —{" "}
              <Link href="/dashboard/sermons/new" className="text-primary hover:underline">
                create your first one
              </Link>
              .
            </CardContent>
          </Card>
        ) : (
          <ul className="divide-border divide-y rounded-lg border">
            {recent.map((s) => (
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
    </div>
  );
}
