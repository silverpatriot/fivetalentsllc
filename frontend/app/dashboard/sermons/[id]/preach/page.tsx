"use client";

import { use, useEffect, useState, type ReactNode } from "react";
import Link from "next/link";
import { ArrowLeft, Loader2 } from "lucide-react";
import { formatDeliveryEstimate } from "@/lib/timing";

type Sermon = {
  id: string;
  title: string;
  content: string | null;
};

// Mirrors ../page.tsx's own CitationFlag — kept as a separate local copy
// rather than a shared module, matching this codebase's existing
// convention (test files duplicate their own fixtures rather than
// sharing a module across files; see e.g. test_sermon_citations.py's
// own docstring).
type CitationFlag = {
  reference: string;
  status: "verified" | "not_quoted" | "invalid_reference" | "quote_mismatch";
  quoted_text: string | null;
  source_text: string | null;
  detail: string;
};

type Span = { start: number; end: number };

// Locates each citation's quoted_text inside the full manuscript by
// plain substring search — the backend only returns the extracted quote
// string, not its offset (see app/services/bible.py's _extract_quoted_near),
// so this is an approximation: if the exact same wording appears more
// than once in the manuscript, indexOf finds the FIRST occurrence, which
// may not be the one actually adjacent to the reference. Acceptable here
// — this only drives cosmetic inline highlighting for reading aloud, not
// anything safety-relevant like the edit-splice guard's exact-offset
// matching.
function findQuoteSpans(content: string, citations: CitationFlag[]): Span[] {
  const spans: Span[] = [];
  for (const c of citations) {
    if (!c.quoted_text) continue;
    const start = content.indexOf(c.quoted_text);
    if (start === -1) continue;
    spans.push({ start, end: start + c.quoted_text.length });
  }
  spans.sort((a, b) => a.start - b.start);
  const merged: Span[] = [];
  for (const span of spans) {
    const last = merged[merged.length - 1];
    if (last && span.start < last.end) continue; // drop an overlapping later match
    merged.push(span);
  }
  return merged;
}

// Renders the manuscript as paragraph blocks (split on the manuscript's
// own "\n\n" — this codebase's paragraph-boundary convention, see
// chunking.py's _SEPARATORS) with scripture quotes visually distinguished
// inline. Typography-only: never re-flows or re-splits the text itself —
// see the Phase 7 Task 1 proposal for why that's one deliberate principle
// covering both the pause-marker and paragraph-reflow questions.
function renderManuscript(content: string, spans: Span[]) {
  const paragraphs: ReactNode[][] = [[]];
  let cursor = 0;

  function pushPlainText(text: string) {
    const parts = text.split("\n\n");
    parts.forEach((part, i) => {
      if (part) paragraphs[paragraphs.length - 1].push(part);
      if (i < parts.length - 1) paragraphs.push([]);
    });
  }

  for (const span of spans) {
    pushPlainText(content.slice(cursor, span.start));
    const quote = content.slice(span.start, span.end);
    paragraphs[paragraphs.length - 1].push(
      // A left-border+padding treatment (the citation panel's own
      // blockquote style, see ../page.tsx's CitationBadge) reads well as
      // a standalone block but not sitting mid-sentence inline — a live
      // screenshot during this pass showed it rendering as a stray bar
      // jammed against the preceding quote mark. Plain italics is the
      // standard, unobtrusive convention for an inline quotation and
      // reads cleanly at reading-aloud size.
      <em key={span.start}>{quote}</em>,
    );
    cursor = span.end;
  }
  pushPlainText(content.slice(cursor));

  return paragraphs
    .filter((nodes) => nodes.length > 0)
    .map((nodes, i) => (
      <p key={i} className="mb-8">
        {nodes}
      </p>
    ));
}

export default function PreachPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [sermon, setSermon] = useState<Sermon | null>(null);
  const [citations, setCitations] = useState<CitationFlag[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    fetch(`/api/sermons/${id}`)
      .then(async (r) => {
        if (!r.ok) throw new Error((await r.json()).detail ?? `Could not load sermon (${r.status})`);
        return r.json();
      })
      .then((data: Sermon) => {
        setSermon(data);
        if (data.content) {
          // Best-effort: a citations failure (e.g. bible-api.com down)
          // shouldn't block reading the manuscript itself — the view
          // just renders without inline scripture highlighting.
          fetch(`/api/sermons/${id}/citations`)
            .then((r) => (r.ok ? r.json() : []))
            .then(setCitations)
            .catch(() => setCitations([]));
        }
      })
      .catch((e) => setLoadError(e instanceof Error ? e.message : "Could not load sermon"));
  }, [id]);

  if (loadError) return <p className="text-destructive p-6 text-sm">{loadError}</p>;
  if (!sermon) {
    return (
      <p className="text-muted-foreground flex items-center gap-1.5 p-6 text-sm">
        <Loader2 className="size-4 animate-spin" /> Loading…
      </p>
    );
  }

  const spans = sermon.content ? findQuoteSpans(sermon.content, citations) : [];

  return (
    <div className="bg-background min-h-screen">
      {/* Deliberately minimal chrome — this view is for standing at a
          pulpit, not editing. Everything the edit page needs (generate
          form, edit panel, citation report) is one tap away via the
          link back, not duplicated here. */}
      <div className="border-border sticky top-0 flex items-center justify-between border-b px-6 py-3 print:hidden">
        <Link
          href={`/dashboard/sermons/${id}`}
          className="text-muted-foreground hover:text-foreground flex items-center gap-1.5 text-sm"
        >
          <ArrowLeft className="size-4" /> Back
        </Link>
        <span className="text-muted-foreground text-xs">{sermon.title}</span>
      </div>

      <div className="mx-auto max-w-3xl px-6 py-10">
        <h1 className="mb-2 text-2xl font-semibold tracking-tight">{sermon.title}</h1>
        {sermon.content && (
          <p className="text-muted-foreground mb-10 text-sm print:hidden">
            {formatDeliveryEstimate(sermon.content)} at an average pace — adjust to your own delivery.
          </p>
        )}
        {sermon.content ? (
          <div className="text-2xl leading-loose">{renderManuscript(sermon.content, spans)}</div>
        ) : (
          <p className="text-muted-foreground text-sm">
            No manuscript yet —{" "}
            <Link href={`/dashboard/sermons/${id}`} className="underline">
              generate one first
            </Link>
            .
          </p>
        )}
      </div>
    </div>
  );
}
