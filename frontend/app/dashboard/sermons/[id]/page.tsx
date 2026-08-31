"use client";

import { use, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";

type Sermon = {
  id: string;
  title: string;
  format: string;
  status: string;
  content: string | null;
};

type CitationFlag = {
  reference: string;
  status: "verified" | "invalid_reference" | "quote_mismatch";
  quoted_text: string | null;
  source_text: string | null;
  detail: string;
};

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
  const style =
    flag.status === "verified"
      ? "bg-secondary text-secondary-foreground"
      : "bg-destructive/10 text-destructive";
  return (
    <div className={`rounded-lg px-2.5 py-1.5 text-xs ${style}`}>
      <span className="font-medium">{flag.reference}</span> — {flag.detail}
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
  const [outline, setOutline] = useState<string | null>(null);
  const [draftText, setDraftText] = useState("");
  const [citations, setCitations] = useState<CitationFlag[] | null>(null);
  const draftRef = useRef("");

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
    setOutline(null);
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
            setOutline((frame.data as { text: string }).text);
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

  if (loadError) return <p className="text-destructive text-sm">{loadError}</p>;
  if (!sermon) return <p className="text-muted-foreground text-sm">Loading…</p>;

  const flaggedCount = citations?.filter((c) => c.status !== "verified").length ?? 0;

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
            <input
              value={passageReference}
              onChange={(e) => setPassageReference(e.target.value)}
              placeholder="Philippians 4:11-13"
              className="border-input rounded-lg border bg-transparent px-2.5 py-1.5 text-sm outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
            />
          </label>
          <label className="flex flex-col gap-1.5 text-sm">
            Topic (optional if a passage is given)
            <input
              value={topic}
              onChange={(e) => setTopic(e.target.value)}
              placeholder="Contentment"
              className="border-input rounded-lg border bg-transparent px-2.5 py-1.5 text-sm outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
            />
          </label>
          <Button type="submit" disabled={generating || (!passageReference && !topic)}>
            {generating ? "Generating…" : "Generate sermon"}
          </Button>
        </form>
      )}

      {genError && (
        <div className="bg-destructive/10 text-destructive rounded-lg p-3 text-sm">{genError}</div>
      )}

      {outline && (
        <div>
          <h2 className="text-sm font-semibold">Outline</h2>
          <pre className="text-muted-foreground mt-1 whitespace-pre-wrap text-sm">{outline}</pre>
        </div>
      )}

      {(draftText || sermon.content) && (
        <div>
          <h2 className="text-sm font-semibold">Draft</h2>
          <div className="mt-1 whitespace-pre-wrap text-sm leading-relaxed">
            {draftText || sermon.content}
            {generating && <span className="animate-pulse">▍</span>}
          </div>
        </div>
      )}

      {citations && citations.length > 0 && (
        <div className="flex flex-col gap-2">
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
