"use client";

// Concordance: exact/stemmed word search across scripture ("every verse
// containing 'grace'") — deliberately NOT semantic search, see
// backend/app/services/concordance.py's docstring for why. Its own
// standalone nav entry, sibling to Study/Compare — same pattern as every
// other dashboard feature (see dashboard/layout.tsx's nav comment).
import { useEffect, useState } from "react";
import { Search } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent } from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

type TranslationOption = { code: string; label: string };

type ConcordanceVerse = { reference: string; text: string; translation: string };
type ConcordanceWebResult = { title: string; url: string; content: string };

type ConcordanceResponse = {
  query: string;
  translation: string;
  local_matches: ConcordanceVerse[];
  web_results: ConcordanceWebResult[];
  used_web_search: boolean;
};

export default function ConcordancePage() {
  const [translations, setTranslations] = useState<TranslationOption[] | null>(null);
  const [translation, setTranslation] = useState("kjv");

  const [query, setQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ConcordanceResponse | null>(null);

  useEffect(() => {
    // Reuses /api/bible/translations (Phase 2's comparison-view catalog)
    // rather than a second, duplicate translation list.
    fetch("/api/bible/translations")
      .then((r) => (r.ok ? r.json() : null))
      .then((data: { translations: TranslationOption[] } | null) => {
        if (data) setTranslations(data.translations);
      });
  }, []);

  async function handleSearch(e: React.FormEvent) {
    e.preventDefault();
    setSearching(true);
    setError(null);
    setResult(null);
    try {
      const params = new URLSearchParams({ q: query, translation });
      const resp = await fetch(`/api/concordance/search?${params.toString()}`);
      if (!resp.ok) {
        const data = await resp.json().catch(() => null);
        throw new Error(data?.detail ? String(data.detail) : `Search failed (${resp.status})`);
      }
      setResult(await resp.json());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Search failed");
    } finally {
      setSearching(false);
    }
  }

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-6">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Concordance</h1>
        <p className="text-muted-foreground mt-1 text-sm">
          Find every verse containing a word — exact and stemmed matches, not a topic search.
        </p>
      </div>

      <form onSubmit={handleSearch} className="flex items-center gap-2">
        <div className="relative flex-1">
          <Search className="text-muted-foreground pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2" />
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            required
            placeholder="grace"
            className="pl-8"
          />
        </div>
        {translations && translations.length > 0 && (
          <Select value={translation} onValueChange={(value) => value && setTranslation(value)}>
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {translations.map((t) => (
                <SelectItem key={t.code} value={t.code}>
                  {t.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
        <Button type="submit" disabled={searching}>
          {searching ? "Searching…" : "Search"}
        </Button>
      </form>

      {error && <p className="text-destructive text-sm">{error}</p>}

      {result && (
        <div className="flex flex-col gap-6">
          {result.local_matches.length === 0 ? (
            <p className="text-muted-foreground text-sm">No matches in {result.translation.toUpperCase()}.</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-32">Reference</TableHead>
                  <TableHead>Text</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {result.local_matches.map((m) => (
                  <TableRow key={m.reference}>
                    <TableCell className="align-top font-medium whitespace-nowrap">{m.reference}</TableCell>
                    <TableCell className="whitespace-normal">{m.text}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}

          {result.used_web_search && result.web_results.length > 0 && (
            <div className="flex flex-col gap-2">
              <h2 className="text-muted-foreground text-xs font-semibold tracking-wide uppercase">
                From live web search — not verified against scripture text directly, and not local matches
              </h2>
              {result.web_results.map((r) => (
                <Card key={r.url} className="border-dashed">
                  <CardContent className="text-sm">
                    <a href={r.url} target="_blank" rel="noreferrer" className="font-medium hover:underline">
                      {r.title}
                    </a>
                    <p className="text-muted-foreground mt-0.5 line-clamp-2">{r.content}</p>
                  </CardContent>
                </Card>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
