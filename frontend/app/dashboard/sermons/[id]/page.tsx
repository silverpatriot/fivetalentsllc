"use client";

import { use, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { Download, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { downloadText } from "@/lib/download";

type Sermon = {
  id: string;
  title: string;
  format: string;
  status: string;
  content: string | null;
  // Migration 0010 — a persisted, preachable outline condensed FROM
  // `content` on demand (see handleCreateOutline below). Distinct from
  // draftOutline (this component's own state, below): that one is the
  // ephemeral pre-manuscript outline streamed during generation itself,
  // never saved.
  outline: string | null;
};

type CitationFlag = {
  reference: string;
  // "not_quoted": the reference resolves but nothing in the draft
  // directly quotes it (bare parenthetical, or indirect/paraphrased
  // prose) — nothing was checked, so nothing failed. Distinct from
  // "verified" (wording actually checked and matched); grouped with it
  // for styling/counting purposes below (see OK_CITATION_STATUSES).
  status: "verified" | "not_quoted" | "invalid_reference" | "quote_mismatch";
  quoted_text: string | null;
  source_text: string | null;
  detail: string;
};

// Mirrors app/services/generation.py's _OK_CITATION_STATUSES — statuses
// that mean "nothing wrong here", not a real problem to flag.
const OK_CITATION_STATUSES = new Set(["verified", "not_quoted"]);

// Parses the backend's Server-Sent-Events stream (see
// app/services/generation.py's _sse()) into discrete {event, data}
// frames as they arrive — SSE frames are separated by a blank line, and
// each may carry multiple `data:` lines, though this backend only ever
// sends one per frame.
function parseSseChunk(buffer: string): { frames: { event: string; data: unknown }[]; rest: string } {
  const frames: { event: string; data: unknown }[] = [];
  const parts = buffer.split("\n\n");
  const rest = parts.pop() ?? "";
  for (const part of parts) {
    let event = "message";
    const dataLines: string[] = [];
    for (const line of part.split("\n")) {
      if (line.startsWith("event:")) event = line.slice("event:".length).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice("data:".length).trim());
    }
    if (dataLines.length === 0) continue;
    try {
      frames.push({ event, data: JSON.parse(dataLines.join("\n")) });
    } catch {
      // Ignore an unparseable frame rather than breaking the whole stream.
    }
  }
  return { frames, rest };
}

function CitationBadge({ flag }: { flag: CitationFlag }) {
  const style = OK_CITATION_STATUSES.has(flag.status)
    ? "bg-secondary text-secondary-foreground"
    : "bg-destructive/10 text-destructive";
  return (
    <div className={`rounded-lg px-2.5 py-1.5 text-xs ${style}`}>
      <span className="font-medium">{flag.reference}</span> — {flag.detail}
      {/* flag.source_text is the real, verified scripture text
          bible.verify_all_citations already resolved server-side — it
          was being computed and sent every time but never rendered. */}
      {flag.source_text && (
        <blockquote className="text-muted-foreground mt-1 border-l-2 border-current/20 pl-2 italic">
          {flag.source_text}
        </blockquote>
      )}
    </div>
  );
}

export default function SermonDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);

  const [sermon, setSermon] = useState<Sermon | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [passageReference, setPassageReference] = useState("");
  const [topic, setTopic] = useState("");
  const [generating, setGenerating] = useState(false);
  const [genError, setGenError] = useState<string | null>(null);
  // The ephemeral, pre-manuscript outline pass every generation runs
  // internally (context_assembly.build_outline_messages) — streamed here
  // for feedback during generation, never persisted on its own. NOT the
  // same thing as sermon.outline (the new, real, saved preaching
  // outline) — kept under a clearly distinct name so the two don't
  // collide in this component the way they used to share one "outline"
  // name before that field existed.
  const [draftOutline, setDraftOutline] = useState<string | null>(null);
  const [draftText, setDraftText] = useState("");
  const [citations, setCitations] = useState<CitationFlag[] | null>(null);
  const draftRef = useRef("");

  const [creatingOutline, setCreatingOutline] = useState(false);
  const [outlineError, setOutlineError] = useState<string | null>(null);

  // Phase 6: iterative, chat-style editing of the already-generated
  // draft above. manuscriptRef + editSelection capture a real text
  // selection the pastor makes IN the rendered draft — see
  // handleManuscriptMouseUp — which becomes the exact {start, end}
  // character offsets POST /sermons/{id}/edit uses to scope the edit
  // (Task 1.1's confirmed design). Without a selection, the backend
  // locates the target itself instead.
  const manuscriptRef = useRef<HTMLDivElement>(null);
  const [editSelection, setEditSelection] = useState<{ start: number; end: number; text: string } | null>(null);
  const [editInstruction, setEditInstruction] = useState("");
  const [editing, setEditing] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);
  // The span the backend actually locked onto (echoed back via the
  // `target` SSE event) — same thing as editSelection when the pastor
  // selected text themselves, but this is the only way to see it when
  // they didn't (the backend's own locate step chose it instead).
  const [editTarget, setEditTarget] = useState<{ start: number; end: number; text: string } | null>(null);
  // The streamed REPLACEMENT text only (never the whole manuscript — see
  // _run_edit's docstring) — shown in its own preview panel rather than
  // spliced into the manuscript live, so the draft never renders in a
  // half-edited state mid-stream. sermon.content only updates once, from
  // the `done` event's authoritative already-spliced full text.
  const [editPreview, setEditPreview] = useState("");
  const editPreviewRef = useRef("");

  useEffect(() => {
    fetch(`/api/sermons/${id}`)
      .then(async (r) => {
        if (!r.ok) throw new Error((await r.json()).detail ?? `Could not load sermon (${r.status})`);
        return r.json();
      })
      .then(setSermon)
      .catch((e) => setLoadError(e instanceof Error ? e.message : "Could not load sermon"));
  }, [id]);

  async function handleGenerate(e: React.FormEvent) {
    e.preventDefault();
    setGenerating(true);
    setGenError(null);
    setDraftOutline(null);
    setDraftText("");
    setCitations(null);
    draftRef.current = "";

    try {
      const resp = await fetch(`/api/sermons/${id}/generate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          passage_reference: passageReference || undefined,
          topic: topic || undefined,
        }),
      });
      if (!resp.ok || resp.body === null) {
        const data = await resp.json().catch(() => ({}));
        throw new Error(data.detail ?? `Generation failed (${resp.status})`);
      }

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const { frames, rest } = parseSseChunk(buffer);
        buffer = rest;
        for (const frame of frames) {
          if (frame.event === "outline") {
            setDraftOutline((frame.data as { text: string }).text);
          } else if (frame.event === "delta") {
            draftRef.current += (frame.data as { text: string }).text;
            setDraftText(draftRef.current);
          } else if (frame.event === "citations") {
            setCitations((frame.data as { flags: CitationFlag[] }).flags);
          } else if (frame.event === "error") {
            setGenError((frame.data as { detail: string }).detail);
          } else if (frame.event === "done") {
            setSermon((s) => (s ? { ...s, status: "ready", content: draftRef.current } : s));
          }
        }
      }
    } catch (e) {
      setGenError(e instanceof Error ? e.message : "Generation failed");
    } finally {
      setGenerating(false);
    }
  }

  async function handleCreateOutline() {
    setCreatingOutline(true);
    setOutlineError(null);
    try {
      const resp = await fetch(`/api/sermons/${id}/outline`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      if (!resp.ok) {
        const data = await resp.json().catch(() => null);
        throw new Error(data?.detail ? String(data.detail) : `Could not create outline (${resp.status})`);
      }
      setSermon(await resp.json());
    } catch (err) {
      setOutlineError(err instanceof Error ? err.message : "Could not create outline");
    } finally {
      setCreatingOutline(false);
    }
  }

  // A real text selection inside the rendered manuscript -> the exact
  // character offsets into sermon.content it corresponds to. Standard
  // Range-measurement technique: a second Range spanning the container's
  // start to the selection's start, whose stringified length IS the
  // start offset, because manuscriptRef's only rendered child (once
  // !generating) is `{manuscript}` itself with nothing else mixed in —
  // no icons or extra text nodes to throw the count off.
  function handleManuscriptMouseUp() {
    if (!manuscriptRef.current) return;
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return;
    const range = sel.getRangeAt(0);
    if (!manuscriptRef.current.contains(range.commonAncestorContainer)) return;
    const text = range.toString();
    if (!text) return;
    const preRange = document.createRange();
    preRange.selectNodeContents(manuscriptRef.current);
    preRange.setEnd(range.startContainer, range.startOffset);
    const start = preRange.toString().length;
    setEditSelection({ start, end: start + text.length, text });
  }

  async function handleEdit(e: React.FormEvent) {
    e.preventDefault();
    if (!editInstruction.trim()) return;
    setEditing(true);
    setEditError(null);
    setEditTarget(null);
    setEditPreview("");
    editPreviewRef.current = "";

    try {
      const resp = await fetch(`/api/sermons/${id}/edit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          instruction: editInstruction,
          selection: editSelection ? { start: editSelection.start, end: editSelection.end } : undefined,
        }),
      });
      if (!resp.ok || resp.body === null) {
        const data = await resp.json().catch(() => ({}));
        throw new Error(data.detail ?? `Edit failed (${resp.status})`);
      }

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const { frames, rest } = parseSseChunk(buffer);
        buffer = rest;
        for (const frame of frames) {
          if (frame.event === "target") {
            setEditTarget(frame.data as { start: number; end: number; text: string });
          } else if (frame.event === "delta") {
            editPreviewRef.current += (frame.data as { text: string }).text;
            setEditPreview(editPreviewRef.current);
          } else if (frame.event === "citations") {
            // Same treatment as original generation's citations block
            // below — an edit re-verifies the WHOLE draft, so this is
            // the up-to-date, complete citation list either way.
            setCitations((frame.data as { flags: CitationFlag[] }).flags);
          } else if (frame.event === "error") {
            setEditError((frame.data as { detail: string }).detail);
          } else if (frame.event === "done") {
            const data = frame.data as { content: string };
            setSermon((s) => (s ? { ...s, content: data.content } : s));
            setEditInstruction("");
            setEditSelection(null);
            setEditTarget(null);
            setEditPreview("");
          }
        }
      }
    } catch (e) {
      setEditError(e instanceof Error ? e.message : "Edit failed");
    } finally {
      setEditing(false);
    }
  }

  if (loadError) return <p className="text-destructive text-sm">{loadError}</p>;
  if (!sermon) {
    return (
      <p className="text-muted-foreground flex items-center gap-1.5 text-sm">
        <Loader2 className="size-4 animate-spin" /> Loading…
      </p>
    );
  }

  const flaggedCount = citations?.filter((c) => !OK_CITATION_STATUSES.has(c.status)).length ?? 0;
  const manuscript = draftText || sermon.content;

  return (
    <div className="mx-auto flex max-w-2xl flex-col gap-6">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">{sermon.title}</h1>
        <p className="text-muted-foreground text-xs capitalize">
          {sermon.format} · {sermon.status}
        </p>
      </div>

      {(sermon.status === "draft" || generating) && !draftText && (
        <form onSubmit={handleGenerate} className="border-border flex flex-col gap-3 rounded-lg border p-4">
          <h2 className="text-sm font-semibold">Generate</h2>
          <label className="flex flex-col gap-1.5 text-sm">
            Scripture passage (optional)
            <Input
              value={passageReference}
              onChange={(e) => setPassageReference(e.target.value)}
              placeholder="Philippians 4:11-13"
            />
          </label>
          <label className="flex flex-col gap-1.5 text-sm">
            Topic (optional if a passage is given)
            <Input value={topic} onChange={(e) => setTopic(e.target.value)} placeholder="Contentment" />
          </label>
          <Button type="submit" disabled={generating || (!passageReference && !topic)}>
            {generating ? "Generating…" : "Generate sermon"}
          </Button>
        </form>
      )}

      {genError && (
        <div className="bg-destructive/10 text-destructive rounded-lg p-3 text-sm">{genError}</div>
      )}

      {draftOutline && (
        <div>
          <h2 className="text-sm font-semibold">Outline</h2>
          <pre className="text-muted-foreground mt-1 whitespace-pre-wrap text-sm">{draftOutline}</pre>
        </div>
      )}

      {manuscript && (
        <div>
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold">Draft</h2>
            {sermon.content && (
              <div className="flex gap-2">
                {/* Phase 7 Task 1: a separate, minimal-chrome view formatted
                    for actually reading aloud at a pulpit — not another
                    section on this already-busy edit page. */}
                <Link href={`/dashboard/sermons/${id}/preach`}>
                  <Button size="sm" variant="outline">
                    Preach
                  </Button>
                </Link>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => downloadText(`${sermon.title}.md`, sermon.content ?? "")}
                >
                  <Download /> Download manuscript
                </Button>
              </div>
            )}
          </div>
          <div
            ref={manuscriptRef}
            onMouseUp={generating ? undefined : handleManuscriptMouseUp}
            className="mt-1 whitespace-pre-wrap text-sm leading-relaxed"
          >
            {manuscript}
            {generating && <span className="animate-pulse">▍</span>}
          </div>
        </div>
      )}

      {/* Phase 6: iterative editing of the draft above — chat-style, on
          the same page (not a separate view), since the pastor needs to
          see the draft while asking for changes to it. */}
      {sermon.content && !generating && (
        <div className="border-border flex flex-col gap-3 rounded-lg border p-4">
          <div>
            <h2 className="text-sm font-semibold">Edit this draft</h2>
            <p className="text-muted-foreground text-xs">
              Select text above to target a specific part, or just describe the change — e.g. &ldquo;make
              point 2 more personal&rdquo;, &ldquo;shorten the introduction&rdquo;.
            </p>
          </div>

          {editSelection && (
            <div className="bg-secondary/50 flex items-start justify-between gap-2 rounded-lg p-2 text-xs">
              <span className="text-muted-foreground italic">
                Selected: &ldquo;
                {editSelection.text.length > 140 ? editSelection.text.slice(0, 140) + "…" : editSelection.text}
                &rdquo;
              </span>
              <button
                type="button"
                className="text-muted-foreground shrink-0 underline"
                onClick={() => setEditSelection(null)}
              >
                Clear
              </button>
            </div>
          )}

          <form onSubmit={handleEdit} className="flex gap-2">
            <Input
              value={editInstruction}
              onChange={(e) => setEditInstruction(e.target.value)}
              placeholder="Make point 2 more personal…"
              disabled={editing}
            />
            <Button type="submit" disabled={editing || !editInstruction.trim()}>
              {editing ? "Revising…" : "Apply edit"}
            </Button>
          </form>

          {editError && (
            <div className="bg-destructive/10 text-destructive rounded-lg p-3 text-sm">{editError}</div>
          )}

          {editTarget && (
            <p className="text-muted-foreground text-xs">
              Revising: <span className="italic">
                &ldquo;{editTarget.text.length > 140 ? editTarget.text.slice(0, 140) + "…" : editTarget.text}&rdquo;
              </span>
            </p>
          )}

          {editPreview && (
            <div className="rounded-lg border p-3 text-sm whitespace-pre-wrap leading-relaxed">
              {editPreview}
              {editing && <span className="animate-pulse">▍</span>}
            </div>
          )}
        </div>
      )}

      {/* Preachable outline (migration 0010) — on-demand, condensed FROM
          the manuscript above, actually persisted (sermon.outline), not
          the ephemeral pre-manuscript pass shown as "Outline" above. */}
      {sermon.content && (
        <div className="flex flex-col gap-2">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold">Preaching outline</h2>
            <div className="flex gap-2">
              {sermon.outline && (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => downloadText(`${sermon.title} - Outline.md`, sermon.outline ?? "")}
                >
                  <Download /> Download outline
                </Button>
              )}
              <Button size="sm" variant="outline" onClick={handleCreateOutline} disabled={creatingOutline}>
                {creatingOutline ? "Creating…" : sermon.outline ? "Regenerate outline" : "Create outline"}
              </Button>
            </div>
          </div>
          <p className="text-muted-foreground text-xs">
            Condensed from the manuscript above for actually preaching from — main points, key phrases, and
            scripture quoted in full, not the full manuscript text.
          </p>
          {outlineError && <p className="text-destructive text-sm">{outlineError}</p>}
          {sermon.outline && (
            <div className="rounded-lg border p-4 text-sm whitespace-pre-wrap">{sermon.outline}</div>
          )}
        </div>
      )}

      {citations && citations.length > 0 && (
        // select-none: this panel is a read-only report, not part of the
        // editable draft above — a selection made here can never become
        // an edit's {start, end} target (handleManuscriptMouseUp is only
        // ever bound to the manuscript div, a separate sibling subtree —
        // see that handler's own comment), so a selection here would
        // otherwise silently do nothing with no feedback at all. Making
        // it genuinely unselectable is honest about that up front,
        // rather than letting a pastor select text here, type an edit
        // instruction, and have it fall through to auto-locate (or a
        // stale prior selection) instead of what they thought they'd
        // targeted.
        <div className="flex flex-col gap-2 select-none">
          <h2 className="text-sm font-semibold">
            Scripture citations
            {flaggedCount > 0 && (
              <span className="text-destructive ml-2 text-xs font-normal">
                {flaggedCount} need review before preaching this
              </span>
            )}
          </h2>
          {citations.map((c, i) => (
            <CitationBadge key={`${c.reference}-${i}`} flag={c} />
          ))}
        </div>
      )}
    </div>
  );
}
