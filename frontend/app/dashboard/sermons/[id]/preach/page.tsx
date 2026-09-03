"use client";

import { use, useEffect, useState, type ReactNode } from "react";
import Link from "next/link";
import { ArrowLeft, Loader2 } from "lucide-react";
import { formatDeliveryEstimate } from "@/lib/timing";

type Sermon = {
  id: string;
  title: string;
  content: string | null;
  // Migration 0010's on-demand, condensed preaching outline — nullable
  // (only created when a pastor explicitly asks for one on the detail
  // page), distinct from `content` (the full manuscript).
  outline: string | null;
};

// Mirrors ../page.tsx's own CitationFlag — kept as a separate local copy
// rather than a shared module, matching this codebase's existing
// convention (test files duplicate their own fixtures rather than
// sharing a module across files; see e.g. test_sermon_citations.py's
// own docstring).
type CitationFlag = {
  reference: string;
  status: "verified" | "not_quoted" | "invalid_reference" | "quote_mismatch" | "unverifiable";
  quoted_text: string | null;
  source_text: string | null;
  detail: string;
};

type Span = { start: number; end: number };

// Locates each citation's quoted_text inside the given text by plain
// substring search — the backend only returns the extracted quote
// string, not its offset (see app/services/bible.py's _extract_quoted_near),
// so this is an approximation: if the exact same wording appears more
// than once, indexOf finds the FIRST occurrence, which may not be the
// one actually adjacent to the reference. Acceptable here — this only
// drives cosmetic inline highlighting for reading aloud, not anything
// safety-relevant like the edit-splice guard's exact-offset matching.
// Generic over any (text, citations) pair — not manuscript-specific,
// despite the name predating the Outline tab — so the same function
// serves both views rather than a second copy.
function findQuoteSpans(text: string, citations: CitationFlag[]): Span[] {
  const spans: Span[] = [];
  for (const c of citations) {
    if (!c.quoted_text) continue;
    const start = text.indexOf(c.quoted_text);
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

// Renders text as paragraph blocks (split on "\n\n" — this codebase's
// paragraph-boundary convention, see chunking.py's _SEPARATORS) with
// scripture quotes visually distinguished inline. Typography-only:
// never re-flows or re-splits the text itself — see the Phase 7 Task 1
// proposal for why that's one deliberate principle covering both the
// pause-marker and paragraph-reflow questions. Same function for both
// the manuscript and the outline (see findQuoteSpans above) — reused,
// not duplicated, per the Manuscript/Outline toggle design checkpoint.
function renderManuscript(text: string, spans: Span[]) {
  const paragraphs: ReactNode[][] = [[]];
  let cursor = 0;

  function pushPlainText(chunk: string) {
    const parts = chunk.split("\n\n");
    parts.forEach((part, i) => {
      if (part) paragraphs[paragraphs.length - 1].push(part);
      if (i < parts.length - 1) paragraphs.push([]);
    });
  }

  for (const span of spans) {
    pushPlainText(text.slice(cursor, span.start));
    const quote = text.slice(span.start, span.end);
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
  pushPlainText(text.slice(cursor));

  return paragraphs
    .filter((nodes) => nodes.length > 0)
    .map((nodes, i) => (
      <p key={i} className="mb-8">
        {nodes}
      </p>
    ));
}

type View = "manuscript" | "outline";

export default function PreachPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [sermon, setSermon] = useState<Sermon | null>(null);
  const [citations, setCitations] = useState<CitationFlag[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  // Defaults to the manuscript — it's guaranteed to exist once a sermon
  // is generated, unlike the outline, which is only created on demand.
  const [view, setView] = useState<View>("manuscript");

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

  const activeText = view === "manuscript" ? sermon.content : sermon.outline;
  const spans = activeText ? findQuoteSpans(activeText, citations) : [];

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
          <div className="mb-8 flex gap-1 print:hidden">
            <button
              type="button"
              onClick={() => setView("manuscript")}
              className={`rounded-lg px-3 py-1.5 text-sm ${
                view === "manuscript" ? "bg-secondary text-secondary-foreground" : "text-muted-foreground hover:text-foreground"
              }`}
            >
              Manuscript
            </button>
            <button
              type="button"
              onClick={() => sermon.outline && setView("outline")}
              disabled={!sermon.outline}
              title={sermon.outline ? undefined : "Not created yet — create one from the sermon's edit page"}
              className={`rounded-lg px-3 py-1.5 text-sm ${
                view === "outline"
                  ? "bg-secondary text-secondary-foreground"
                  : sermon.outline
                    ? "text-muted-foreground hover:text-foreground"
                    : "text-muted-foreground/50 cursor-not-allowed"
              }`}
            >
              Outline
            </button>
          </div>
        )}

        {/* The delivery-time estimate is specifically about reading a
            prepared manuscript verbatim (Phase 7 Task 2) — an outline is
            preached FROM, extemporized around, not read at a fixed pace,
            so its word count doesn't mean the same thing. Shown only for
            the Manuscript view rather than implying a false precision
            for the Outline view. */}
        {view === "manuscript" && sermon.content && (
          <p className="text-muted-foreground mb-10 text-sm print:hidden">
            {formatDeliveryEstimate(sermon.content)} at an average pace — adjust to your own delivery.
          </p>
        )}

        {activeText ? (
          <div className="text-2xl leading-loose">{renderManuscript(activeText, spans)}</div>
        ) : view === "outline" ? (
          <p className="text-muted-foreground text-sm">
            No preaching outline yet —{" "}
            <Link href={`/dashboard/sermons/${id}`} className="underline">
              create one from the sermon page
            </Link>
            .
          </p>
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
