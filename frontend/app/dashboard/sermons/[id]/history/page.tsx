"use client";

import { use, useEffect, useState } from "react";
import Link from "next/link";
import { ArrowLeft, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";

type RevisionSummary = {
  id: string;
  instruction: string | null;
  created_at: string;
  is_current: boolean;
};

type DiffSegment = { op: "equal" | "delete" | "insert"; text: string };

// Mirrors app/services/generation.py's REGENERATION_INSTRUCTION_SENTINEL
// exactly — must stay in sync if that ever changes (same duplication
// convention already used for PREACHING_WORDS_PER_MINUTE in lib/timing.ts).
const REGENERATION_SENTINEL = "(sermon regenerated)";

function revisionLabel(r: RevisionSummary): string {
  if (r.is_current) return "Current version";
  if (r.instruction === REGENERATION_SENTINEL) return "Regenerated";
  if (r.instruction?.startsWith("(restored to revision")) return "Restored from an earlier version";
  const instruction = r.instruction ?? "";
  return `Edit: “${instruction.length > 80 ? instruction.slice(0, 80) + "…" : instruction}”`;
}

function formatTimestamp(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

function DiffView({ diff }: { diff: DiffSegment[] }) {
  return (
    <div className="rounded-lg border p-4 text-sm leading-relaxed whitespace-pre-wrap">
      {diff.map((seg, i) => {
        if (seg.op === "equal") return <span key={i}>{seg.text}</span>;
        if (seg.op === "delete")
          return (
            <span key={i} className="bg-destructive/10 text-destructive line-through decoration-2">
              {seg.text}
            </span>
          );
        return (
          <span key={i} className="bg-secondary text-secondary-foreground underline decoration-2">
            {seg.text}
          </span>
        );
      })}
    </div>
  );
}

export default function SermonHistoryPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [revisions, setRevisions] = useState<RevisionSummary[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [viewingId, setViewingId] = useState<string | null>(null);
  const [viewingContent, setViewingContent] = useState<string | null>(null);
  const [viewLoading, setViewLoading] = useState(false);

  const [compareFrom, setCompareFrom] = useState<string>("");
  const [compareTo, setCompareTo] = useState<string>("current");
  const [diff, setDiff] = useState<DiffSegment[] | null>(null);
  const [compareLoading, setCompareLoading] = useState(false);
  const [compareError, setCompareError] = useState<string | null>(null);

  const [confirmingRestoreId, setConfirmingRestoreId] = useState<string | null>(null);
  const [restoring, setRestoring] = useState(false);
  const [restoreError, setRestoreError] = useState<string | null>(null);
  const [restoreNotice, setRestoreNotice] = useState<string | null>(null);

  function loadRevisions() {
    fetch(`/api/sermons/${id}/revisions`)
      .then(async (r) => {
        if (!r.ok) throw new Error((await r.json()).detail ?? `Could not load history (${r.status})`);
        return r.json();
      })
      .then((data: RevisionSummary[]) => {
        setRevisions(data);
        // Sensible default comparison: the oldest past revision against
        // current — "how does this compare to when it all started".
        const oldestPast = data.filter((r) => !r.is_current).at(-1);
        if (oldestPast) setCompareFrom(oldestPast.id);
      })
      .catch((e) => setLoadError(e instanceof Error ? e.message : "Could not load history"));
  }

  useEffect(loadRevisions, [id]);

  async function handleView(revisionId: string) {
    if (viewingId === revisionId) {
      setViewingId(null);
      setViewingContent(null);
      return;
    }
    setViewingId(revisionId);
    setViewLoading(true);
    setViewingContent(null);
    try {
      const resp = await fetch(`/api/sermons/${id}/revisions/${revisionId}`);
      if (!resp.ok) throw new Error((await resp.json()).detail ?? "Could not load that version");
      setViewingContent((await resp.json()).content);
    } catch (e) {
      setViewingContent(e instanceof Error ? e.message : "Could not load that version");
    } finally {
      setViewLoading(false);
    }
  }

  async function handleCompare() {
    setCompareLoading(true);
    setCompareError(null);
    setDiff(null);
    try {
      const resp = await fetch(
        `/api/sermons/${id}/revisions/compare?from_id=${encodeURIComponent(compareFrom)}&to_id=${encodeURIComponent(compareTo)}`,
      );
      if (!resp.ok) throw new Error((await resp.json()).detail ?? "Could not compare those versions");
      setDiff((await resp.json()).diff);
    } catch (e) {
      setCompareError(e instanceof Error ? e.message : "Could not compare those versions");
    } finally {
      setCompareLoading(false);
    }
  }

  async function handleRestore(revisionId: string) {
    setRestoring(true);
    setRestoreError(null);
    setRestoreNotice(null);
    try {
      const resp = await fetch(`/api/sermons/${id}/revisions/${revisionId}/restore`, { method: "POST" });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail ?? "Could not restore that version");
      setRestoreNotice("Restored — this is now the current version. A snapshot of what was current before this restore was saved too, so this can be undone the same way.");
      setConfirmingRestoreId(null);
      setViewingId(null);
      setDiff(null);
      loadRevisions();
    } catch (e) {
      setRestoreError(e instanceof Error ? e.message : "Could not restore that version");
    } finally {
      setRestoring(false);
    }
  }

  if (loadError) return <p className="text-destructive p-6 text-sm">{loadError}</p>;
  if (!revisions) {
    return (
      <p className="text-muted-foreground flex items-center gap-1.5 p-6 text-sm">
        <Loader2 className="size-4 animate-spin" /> Loading…
      </p>
    );
  }

  return (
    <div className="mx-auto flex max-w-2xl flex-col gap-6 p-6">
      <div className="flex items-center justify-between">
        <Link href={`/dashboard/sermons/${id}`} className="text-muted-foreground hover:text-foreground flex items-center gap-1.5 text-sm">
          <ArrowLeft className="size-4" /> Back
        </Link>
        <h1 className="text-sm font-semibold">Version history</h1>
      </div>

      {restoreNotice && <div className="bg-secondary/50 rounded-lg p-3 text-sm">{restoreNotice}</div>}

      {revisions.length <= 1 ? (
        <p className="text-muted-foreground text-sm">
          No edit history yet — this draft hasn&rsquo;t been edited or regenerated since it was generated.
        </p>
      ) : (
        <>
          <div className="border-border flex flex-col gap-2 rounded-lg border p-4">
            <h2 className="text-sm font-semibold">Compare</h2>
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <select
                className="border-border rounded-lg border bg-transparent px-2 py-1.5 text-sm"
                value={compareFrom}
                onChange={(e) => setCompareFrom(e.target.value)}
              >
                {revisions.map((r) => (
                  <option key={r.id} value={r.id}>
                    {revisionLabel(r)} — {formatTimestamp(r.created_at)}
                  </option>
                ))}
              </select>
              <span className="text-muted-foreground">vs</span>
              <select
                className="border-border rounded-lg border bg-transparent px-2 py-1.5 text-sm"
                value={compareTo}
                onChange={(e) => setCompareTo(e.target.value)}
              >
                {revisions.map((r) => (
                  <option key={r.id} value={r.id}>
                    {revisionLabel(r)} — {formatTimestamp(r.created_at)}
                  </option>
                ))}
              </select>
              <Button size="sm" onClick={handleCompare} disabled={compareLoading || !compareFrom || !compareTo}>
                {compareLoading ? "Comparing…" : "Show diff"}
              </Button>
            </div>
            {compareError && <p className="text-destructive text-sm">{compareError}</p>}
            {diff && (
              <>
                <p className="text-muted-foreground text-xs">
                  <span className="bg-destructive/10 text-destructive px-1 line-through">removed</span>{" "}
                  <span className="bg-secondary text-secondary-foreground px-1 underline">added</span>
                </p>
                <DiffView diff={diff} />
              </>
            )}
          </div>

          <div className="flex flex-col gap-2">
            {revisions.map((r) => (
              <div key={r.id} className="border-border rounded-lg border p-3">
                <div className="flex items-center justify-between gap-2">
                  <div>
                    <p className="text-sm font-medium">{revisionLabel(r)}</p>
                    <p className="text-muted-foreground text-xs">{formatTimestamp(r.created_at)}</p>
                  </div>
                  <div className="flex shrink-0 gap-2">
                    <Button size="sm" variant="outline" onClick={() => handleView(r.id)}>
                      {viewingId === r.id ? "Hide" : "View"}
                    </Button>
                    {!r.is_current && confirmingRestoreId !== r.id && (
                      <Button size="sm" variant="outline" onClick={() => setConfirmingRestoreId(r.id)}>
                        Restore
                      </Button>
                    )}
                    {!r.is_current && confirmingRestoreId === r.id && (
                      <>
                        <Button size="sm" onClick={() => handleRestore(r.id)} disabled={restoring}>
                          {restoring ? "Restoring…" : "Confirm restore"}
                        </Button>
                        <Button size="sm" variant="outline" onClick={() => setConfirmingRestoreId(null)} disabled={restoring}>
                          Cancel
                        </Button>
                      </>
                    )}
                  </div>
                </div>
                {restoreError && confirmingRestoreId === r.id && <p className="text-destructive mt-2 text-sm">{restoreError}</p>}
                {viewingId === r.id && (
                  <div className="mt-3 rounded-lg border p-3 text-sm whitespace-pre-wrap">
                    {viewLoading ? "Loading…" : viewingContent}
                  </div>
                )}
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
