"use client";

// Multi-version Bible comparison — the same passage, several translations
// side by side. 21 translations are available (backend's API_BIBLE_IDS,
// all live-verified against the real BIBLE_API_KEY), too many for a
// fixed checkbox row or a usable grid all at once, so a sane default
// preset is preselected and the rest are available to add. Reuses
// components/pricing-tiers.tsx's grid-of-cards layout — the only
// existing side-by-side/multi-column template in this app.
import { useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

type TranslationOption = { code: string; label: string };
type Passage = { text: string; translation: string; source: string } | null;
type CompareResponse = { reference: string; passages: Record<string, Passage> };

// KJV + ASV + WEB + NIV + NLT — a sane, recognizable default rather than
// defaulting to all 21 (which wouldn't fit any usable grid).
const DEFAULT_TRANSLATIONS = ["kjv", "asv", "web", "niv11", "nlt"];

export default function ComparePage() {
  const [translations, setTranslations] = useState<TranslationOption[] | null>(null);
  const [selected, setSelected] = useState<string[]>(DEFAULT_TRANSLATIONS);

  const [reference, setReference] = useState("");
  const [comparing, setComparing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<CompareResponse | null>(null);

  useEffect(() => {
    fetch("/api/bible/translations")
      .then((r) => (r.ok ? r.json() : null))
      .then((data: { translations: TranslationOption[] } | null) => {
        if (data) setTranslations(data.translations);
      });
  }, []);

  function toggle(code: string) {
    setSelected((prev) => (prev.includes(code) ? prev.filter((c) => c !== code) : [...prev, code]));
  }

  async function handleCompare(e: React.FormEvent) {
    e.preventDefault();
    setComparing(true);
    setError(null);
    setResult(null);
    try {
      const params = new URLSearchParams({ reference, translations: selected.join(",") });
      const resp = await fetch(`/api/bible/compare?${params.toString()}`);
      if (!resp.ok) {
        const data = await resp.json().catch(() => null);
        throw new Error(data?.detail ? String(data.detail) : `Comparison failed (${resp.status})`);
      }
      setResult(await resp.json());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Comparison failed");
    } finally {
      setComparing(false);
    }
  }

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-6">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Compare translations</h1>
        <p className="text-muted-foreground mt-1 text-sm">
          The same passage, side by side, across the translations you pick.
        </p>
      </div>

      <form onSubmit={handleCompare} className="flex flex-col gap-3">
        <div className="flex items-center gap-2">
          <Input
            value={reference}
            onChange={(e) => setReference(e.target.value)}
            required
            placeholder="John 3:16"
            className="max-w-xs"
          />
          <Button type="submit" disabled={comparing || selected.length === 0}>
            {comparing ? "Comparing…" : "Compare"}
          </Button>
        </div>

        {translations && translations.length > 0 && (
          <div className="flex flex-col gap-1.5">
            <span className="text-muted-foreground text-xs font-semibold tracking-wide uppercase">
              Translations ({selected.length} selected)
            </span>
            <div className="flex flex-wrap gap-1.5">
              {translations.map((t) => (
                <button key={t.code} type="button" onClick={() => toggle(t.code)}>
                  <Badge variant={selected.includes(t.code) ? "default" : "outline"}>{t.label}</Badge>
                </button>
              ))}
            </div>
          </div>
        )}
      </form>

      {error && <p className="text-destructive text-sm">{error}</p>}

      {result && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {Object.entries(result.passages).map(([code, passage]) => {
            const label = translations?.find((t) => t.code === code)?.label ?? code.toUpperCase();
            return (
              <div
                key={code}
                className={cn(
                  "flex flex-col gap-2 rounded-xl border p-5",
                  passage ? "border-border" : "border-border opacity-60"
                )}
              >
                <h3 className="text-sm font-semibold">{label}</h3>
                {passage ? (
                  <p className="flex-1 text-sm leading-relaxed whitespace-pre-wrap">{passage.text}</p>
                ) : (
                  <p className="text-muted-foreground flex-1 text-sm">
                    This reference doesn&apos;t resolve in {label}.
                  </p>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
