"use client";

// Standalone "Recordings" section — its own nav entry, not nested under
// Sermons, matching Study/Compare/Concordance's pattern (dashboard/layout.tsx).
// Upload posts straight to the real POST /media pipeline (app/api/media.py:
// stores the raw file, then transcribes synchronously via Groq/OpenAI —
// app/services/transcription.py). A recording can optionally be linked to
// a sermon at upload time (media_files.sermon_id is nullable specifically
// so a recording doesn't have to belong to one), but this page always
// shows every recording regardless of that link.
import { useEffect, useState } from "react";
import { Download, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { downloadText } from "@/lib/download";

type MediaFile = {
  id: string;
  sermon_id: string | null;
  original_filename: string;
  duration_seconds: number | null;
  transcription_status: string;
  transcript_text: string | null;
  created_at: string;
};

type Sermon = { id: string; title: string };

const STATUS_VARIANT: Record<string, "default" | "secondary" | "destructive" | "outline"> = {
  pending: "outline",
  processing: "outline",
  completed: "secondary",
  failed: "destructive",
};

const STATUS_LABEL: Record<string, string> = {
  pending: "Pending",
  processing: "Transcribing…",
  completed: "Transcribed",
  failed: "Transcription failed",
};

// media_files.duration_seconds is stored as a Postgres NUMERIC, so it
// arrives here as a plain number via Pydantic's float coercion — no
// int/float ambiguity to worry about, just formatting.
function formatDuration(seconds: number | null): string {
  if (seconds === null) return "";
  const total = Math.round(seconds);
  const mins = Math.floor(total / 60);
  const secs = total % 60;
  return `${mins}:${secs.toString().padStart(2, "0")}`;
}

// A sentinel, not "" — base-ui's Select treats an empty string value as
// unset, so "no sermon" needs its own real value to round-trip correctly.
const NO_SERMON = "__none__";

export default function RecordingsPage() {
  const [recordings, setRecordings] = useState<MediaFile[] | null>(null);
  const [sermons, setSermons] = useState<Sermon[]>([]);
  const [listError, setListError] = useState<string | null>(null);

  const [selectedSermon, setSelectedSermon] = useState(NO_SERMON);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);

  async function loadRecordings() {
    try {
      const resp = await fetch("/api/media");
      if (!resp.ok) {
        const data = await resp.json().catch(() => null);
        throw new Error(data?.detail ? String(data.detail) : `Could not load recordings (${resp.status})`);
      }
      setRecordings(await resp.json());
    } catch (err) {
      setListError(err instanceof Error ? err.message : "Could not load recordings");
    }
  }

  async function loadSermons() {
    const resp = await fetch("/api/sermons");
    if (resp.ok) setSermons(await resp.json());
  }

  useEffect(() => {
    loadRecordings();
    loadSermons();
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
      if (selectedSermon !== NO_SERMON) formData.append("sermon_id", selectedSermon);
      const resp = await fetch("/api/media", { method: "POST", body: formData });
      if (!resp.ok) {
        // The error body isn't guaranteed to be JSON (an expired/missing
        // Clerk session makes middleware.ts's auth.protect() 404 with an
        // HTML page here) — same reasoning as dashboard/study/page.tsx.
        const data = await resp.json().catch(() => null);
        throw new Error(data?.detail ? String(data.detail) : `Upload failed (${resp.status})`);
      }
      form.reset();
      setSelectedSermon(NO_SERMON);
      await loadRecordings();
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  }

  async function handleDelete(id: string) {
    await fetch(`/api/media/${id}`, { method: "DELETE" });
    await loadRecordings();
  }

  const sermonTitle = (sermonId: string | null) => sermons.find((s) => s.id === sermonId)?.title ?? null;

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-8">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Recordings</h1>
        <p className="text-muted-foreground mt-1 text-sm">
          Upload sermon audio to get an automatic transcript — Groq&apos;s Whisper by default, falling back to
          OpenAI&apos;s if that&apos;s unavailable. Optionally attach a recording to one of your sermons.
        </p>
      </div>

      <section className="flex flex-col gap-3">
        <form onSubmit={handleUpload} className="border-border flex flex-col gap-3 rounded-lg border p-4">
          <div className="flex flex-wrap items-center gap-2">
            <input
              type="file"
              name="file"
              accept="audio/*,.mp3,.wav,.m4a,.mp4,.ogg,.flac,.webm"
              required
              className="text-sm"
            />
            <Select value={selectedSermon} onValueChange={(v) => v && setSelectedSermon(v)}>
              <SelectTrigger className="w-56">
                <SelectValue placeholder="No sermon" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={NO_SERMON}>No sermon</SelectItem>
                {sermons.map((s) => (
                  <SelectItem key={s.id} value={s.id}>
                    {s.title}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Button type="submit" disabled={uploading}>
              {uploading ? "Uploading…" : "Upload"}
            </Button>
          </div>
          <p className="text-muted-foreground text-xs">
            Up to 25MB. Transcription runs immediately and this may take a few seconds for a long recording.
          </p>
        </form>
        {uploadError && <p className="text-destructive text-sm">{uploadError}</p>}
      </section>

      <section className="flex flex-col gap-3">
        {listError && <p className="text-destructive text-sm">{listError}</p>}
        {recordings === null ? (
          <p className="text-muted-foreground text-sm">Loading…</p>
        ) : recordings.length === 0 ? (
          <p className="text-muted-foreground text-sm">No recordings yet — upload your first one above.</p>
        ) : (
          <ul className="flex flex-col gap-4">
            {recordings.map((r) => (
              <li key={r.id} className="border-border flex flex-col gap-3 rounded-lg border p-4">
                <div className="flex items-start justify-between gap-3">
                  <div className="flex flex-col gap-1">
                    <span className="text-sm font-medium">{r.original_filename}</span>
                    <div className="text-muted-foreground flex flex-wrap items-center gap-2 text-xs">
                      <Badge variant={STATUS_VARIANT[r.transcription_status] ?? "outline"}>
                        {STATUS_LABEL[r.transcription_status] ?? r.transcription_status}
                      </Badge>
                      {r.duration_seconds !== null && <span>{formatDuration(r.duration_seconds)}</span>}
                      <span>{new Date(r.created_at).toLocaleString()}</span>
                      {sermonTitle(r.sermon_id) && <span>· {sermonTitle(r.sermon_id)}</span>}
                    </div>
                  </div>
                  <button
                    onClick={() => handleDelete(r.id)}
                    className="text-muted-foreground hover:text-destructive shrink-0"
                    aria-label="Delete recording"
                  >
                    <Trash2 className="size-4" />
                  </button>
                </div>

                <audio controls src={`/api/media/${r.id}/audio`} className="h-8 w-full" />

                {r.transcription_status === "completed" && r.transcript_text !== null && (
                  <div className="flex flex-col gap-1.5">
                    <div className="flex items-center justify-between">
                      <span className="text-muted-foreground text-xs font-semibold tracking-wide uppercase">
                        Transcript
                      </span>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => downloadText(`${r.original_filename}.txt`, r.transcript_text ?? "", "text/plain")}
                      >
                        <Download /> Download
                      </Button>
                    </div>
                    <p className="text-sm whitespace-pre-wrap">
                      {r.transcript_text || <span className="text-muted-foreground italic">(silence — no speech detected)</span>}
                    </p>
                  </div>
                )}

                {r.transcription_status === "failed" && (
                  <p className="text-destructive text-sm">
                    Transcription failed. The recording itself is safe — you can still play it back above.
                  </p>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
