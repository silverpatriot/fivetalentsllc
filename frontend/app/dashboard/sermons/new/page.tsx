"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

const FORMATS = [
  { value: "expository", label: "Expository — verse-by-verse through a passage" },
  { value: "topical", label: "Topical — organized around a theme" },
  { value: "narrative", label: "Narrative — told as a story arc" },
  { value: "textual", label: "Textual — divisions drawn from a short passage" },
  { value: "custom", label: "Custom — no fixed template" },
] as const;

export default function NewSermonPage() {
  const router = useRouter();
  const [title, setTitle] = useState("");
  const [format, setFormat] = useState<(typeof FORMATS)[number]["value"]>("expository");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const resp = await fetch("/api/sermons", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title, format }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail ? JSON.stringify(data.detail) : "Could not create sermon");
      router.push(`/dashboard/sermons/${data.id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not create sermon");
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="mx-auto flex max-w-md flex-col gap-4">
      <h1 className="text-xl font-semibold tracking-tight">New sermon</h1>

      <label className="flex flex-col gap-1.5 text-sm">
        Title
        <Input
          required
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="On the Love of God"
        />
      </label>

      <label className="flex flex-col gap-1.5 text-sm">
        Format
        <Select value={format} onValueChange={(v) => setFormat(v as typeof format)}>
          <SelectTrigger className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {FORMATS.map((f) => (
              <SelectItem key={f.value} value={f.value}>
                {f.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </label>

      {error && <p className="text-destructive text-sm">{error}</p>}

      <Button type="submit" disabled={submitting}>
        {submitting ? "Creating…" : "Create sermon"}
      </Button>
    </form>
  );
}
