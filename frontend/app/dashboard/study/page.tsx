"use client";

// Phase 4 Task 3: the theology/study corpus — its own standalone
// section (own nav entry, own page), separate from sermon generation.
// Upload reuses Task 1's shared /documents pipeline as-is (corpus_type
// fixed to "theology" here); the query box calls the study-specific
// /study/query endpoint, which grounds its answer in the pastor's own
// uploaded documents and, when that corpus is thin, live web search —
// the two are always rendered as visually distinct source groups below,
// never blended together, matching the same labeling discipline as
// sermon generation's "supplementary web context" section.
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";

type StudyDocument = {
  id: string;
  title: string;
  original_filename: string | null;
  status: string;
  created_at: string;
};

type StudyCitation = {
  source_type: "document" | "web";
  label: string;
  title: string;
  excerpt: string;
  document_id: string | null;
  url: string | null;
};

type StudyAnswer = {
  answer: string;
  citations: StudyCitation[];
  used_own_documents: boolean;
  used_web_search: boolean;
};

const STATUS_LABEL: Record<string, string> = {
  processing: "Processing…",
  ready: "Ready",
  failed: "Failed to process",
};

export default function StudyPage() {
  const [documents, setDocuments] = useState<StudyDocument[] | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);

  const [question, setQuestion] = useState("");
  const [asking, setAsking] = useState(false);
  const [queryError, setQueryError] = useState<string | null>(null);
  const [result, setResult] = useState<StudyAnswer | null>(null);

  async function loadDocuments() {
    const resp = await fetch("/api/documents?corpus_type=theology");
    if (resp.ok) setDocuments(await resp.json());
  }

  useEffect(() => {
    loadDocuments();
  }, []);

  async function handleUpload(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    const fileInput = form.elements.namedItem("file") as HTMLInputElement;
    const file = fileInput.files?.[0];
    if (!file) return;

    setUploading(true);
    setUploadError(null);
    try {
      const formData = new FormData();
      formData.append("file", file);
      formData.append("corpus_type", "theology");
      const resp = await fetch("/api/documents", { method: "POST", body: formData });
      if (!resp.ok) {
        // Don't assume the error body is JSON — an expired/missing Clerk
        // session makes middleware.ts's auth.protect() 404 with an HTML
        // page here, and resp.json() would throw its own opaque parse
        // error instead of surfacing anything useful.
        const data = await resp.json().catch(() => null);
        throw new Error(data?.detail ? String(data.detail) : `Upload failed (${resp.status})`);
      }
      form.reset();
      await loadDocuments();
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  }

  async function handleDelete(id: string) {
    await fetch(`/api/documents/${id}`, { method: "DELETE" });
    await loadDocuments();
  }

  async function handleAsk(e: React.FormEvent) {
    e.preventDefault();
    setAsking(true);
    setQueryError(null);
    setResult(null);
    try {
      const resp = await fetch("/api/study/query", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question }),
      });
      if (!resp.ok) {
        // Same reasoning as handleUpload above — the error body isn't
        // guaranteed to be JSON, so check status before parsing.
        const data = await resp.json().catch(() => null);
        throw new Error(data?.detail ? String(data.detail) : `Could not answer this question (${resp.status})`);
      }
      const data = await resp.json();
      setResult(data);
    } catch (err) {
      setQueryError(err instanceof Error ? err.message : "Could not answer this question");
    } finally {
      setAsking(false);
    }
  }

  const documentCitations = result?.citations.filter((c) => c.source_type === "document") ?? [];
  const webCitations = result?.citations.filter((c) => c.source_type === "web") ?? [];

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-8">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Study</h1>
        <p className="text-muted-foreground mt-1 text-sm">
          Your private theological reference library — upload notes, commentaries, or papers, then ask
          questions grounded in what you&apos;ve uploaded.
        </p>
      </div>

      {/* --- Upload --- */}
      <section className="flex flex-col gap-3">
        <h2 className="text-sm font-semibold">Your documents</h2>
        <form onSubmit={handleUpload} className="flex items-center gap-2">
          <input
            type="file"
            name="file"
            accept=".pdf,.docx,.txt,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/plain"
            required
            className="text-sm"
          />
          <Button type="submit" disabled={uploading}>
            {uploading ? "Uploading…" : "Upload"}
          </Button>
        </form>
        <p className="text-muted-foreground text-xs">
          PDF, DOCX, or plain text. Files are processed into your library and not stored as-is — there&apos;s
          no original file to re-download later, only what&apos;s been extracted and indexed.
        </p>
        {uploadError && <p className="text-destructive text-sm">{uploadError}</p>}

        {documents === null ? (
          <p className="text-muted-foreground text-sm">Loading…</p>
        ) : documents.length === 0 ? (
          <p className="text-muted-foreground text-sm">Nothing uploaded yet.</p>
        ) : (
          <ul className="divide-border divide-y rounded-lg border">
            {documents.map((d) => (
              <li key={d.id} className="flex items-center justify-between px-4 py-3 text-sm">
                <div className="flex flex-col">
                  <span className="font-medium">{d.title}</span>
                  <span className="text-muted-foreground text-xs">{STATUS_LABEL[d.status] ?? d.status}</span>
                </div>
                <button
                  onClick={() => handleDelete(d.id)}
                  className="text-muted-foreground hover:text-destructive text-xs"
                >
                  Remove
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* --- Ask --- */}
      <section className="flex flex-col gap-3">
        <h2 className="text-sm font-semibold">Ask a question</h2>
        <form onSubmit={handleAsk} className="flex flex-col gap-2">
          <textarea
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            required
            rows={3}
            placeholder="What does this material say about justification by faith?"
            className="border-input rounded-lg border bg-transparent px-2.5 py-1.5 text-sm outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
          />
          <Button type="submit" disabled={asking} className="self-start">
            {asking ? "Thinking…" : "Ask"}
          </Button>
        </form>
        {queryError && <p className="text-destructive text-sm">{queryError}</p>}

        {result && (
          <div className="flex flex-col gap-4 rounded-lg border p-4">
            <p className="text-sm whitespace-pre-wrap">{result.answer}</p>

            {documentCitations.length > 0 && (
              <div className="flex flex-col gap-1.5">
                <h3 className="text-muted-foreground text-xs font-semibold tracking-wide uppercase">
                  From your documents
                </h3>
                <ul className="flex flex-col gap-1.5">
                  {documentCitations.map((c) => (
                    <li key={c.label} className="text-xs">
                      <span className="text-muted-foreground">{c.label}</span>{" "}
                      <span className="font-medium">{c.title}</span>
                      <p className="text-muted-foreground mt-0.5 line-clamp-2">{c.excerpt}</p>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {webCitations.length > 0 && (
              <div className="flex flex-col gap-1.5">
                <h3 className="text-muted-foreground text-xs font-semibold tracking-wide uppercase">
                  From live web search — not your own documents, unverified
                </h3>
                <ul className="flex flex-col gap-1.5">
                  {webCitations.map((c) => (
                    <li key={c.label} className="text-xs">
                      <span className="text-muted-foreground">{c.label}</span>{" "}
                      <a href={c.url ?? "#"} target="_blank" rel="noreferrer" className="font-medium hover:underline">
                        {c.title}
                      </a>
                      <p className="text-muted-foreground mt-0.5 line-clamp-2">{c.excerpt}</p>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}
      </section>
    </div>
  );
}
