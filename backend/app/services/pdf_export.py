"""Phase 7 Task 3: PDF export, formatted using Task 1's preaching-view
typographic intent (large type, generous leading, italic scripture
quotes, real paragraph spacing) — NOT literally sharing markup with
frontend/app/dashboard/sermons/[id]/preach/page.tsx, since WeasyPrint
renders plain HTML+CSS, not React/Tailwind/JS. This module rebuilds that
same design intent as a small, self-contained HTML+CSS template instead.

Preaching-format only (no separate raw-manuscript PDF variant) — the
existing "Download manuscript" .md download already serves the personal-
records/sharing case a raw export would, per the Phase 7 Task 3 design
checkpoint.
"""
import html
from dataclasses import dataclass

from starlette.concurrency import run_in_threadpool
from weasyprint import HTML

from app.services.context_assembly import PREACHING_WORDS_PER_MINUTE


@dataclass
class _Span:
    start: int
    end: int


def _find_quote_spans(content: str, citations: list[dict]) -> list[_Span]:
    """Same approach (and same acceptable limitation) as the preach
    view's own findQuoteSpans in page.tsx: locates each citation's
    quoted_text by plain substring search (the backend only returns the
    extracted quote string, not its offset — see bible.py's
    _extract_quoted_near), so an identical wording appearing more than
    once in the manuscript highlights its FIRST occurrence, which may
    not be the one actually adjacent to the reference. Fine here — this
    only drives cosmetic inline italics, not anything safety-relevant.
    """
    spans: list[_Span] = []
    for citation in citations:
        quoted = citation.get("quoted_text")
        if not quoted:
            continue
        start = content.find(quoted)
        if start == -1:
            continue
        spans.append(_Span(start, start + len(quoted)))
    spans.sort(key=lambda s: s.start)
    merged: list[_Span] = []
    for span in spans:
        if merged and span.start < merged[-1].end:
            continue  # drop an overlapping later match
        merged.append(span)
    return merged


def _render_paragraphs_html(content: str, spans: list[_Span]) -> str:
    """Mirrors the preach view's renderManuscript: splits on the
    manuscript's own "\n\n" (this codebase's paragraph-boundary
    convention, see chunking.py's _SEPARATORS) into <p> blocks, wraps
    quoted spans in <em>. Typography-only, like the on-screen view —
    never re-flows or re-splits the text itself. Every plain-text chunk
    is HTML-escaped individually (this is model-generated/user-supplied
    content going straight into an HTML document WeasyPrint parses)."""
    paragraphs: list[list[str]] = [[]]
    cursor = 0

    def push_plain(text: str) -> None:
        parts = text.split("\n\n")
        for i, part in enumerate(parts):
            if part:
                paragraphs[-1].append(html.escape(part))
            if i < len(parts) - 1:
                paragraphs.append([])

    for span in spans:
        push_plain(content[cursor : span.start])
        quote = content[span.start : span.end]
        paragraphs[-1].append(f"<em>{html.escape(quote)}</em>")
        cursor = span.end
    push_plain(content[cursor:])

    return "\n".join(f"<p>{''.join(nodes)}</p>" for nodes in paragraphs if nodes)


def estimate_delivery_minutes(content: str) -> int:
    """Same PREACHING_WORDS_PER_MINUTE as generation's own length target
    and the frontend's mirrored copy (see lib/timing.ts) — one number,
    three consumers, never independently re-derived."""
    words = len(content.split())
    return round(words / PREACHING_WORDS_PER_MINUTE)


_CSS = """
@page { size: letter; margin: 1in; }
body {
    font-family: "Liberation Serif", Georgia, serif;
    font-size: 13pt;
    line-height: 1.8;
    color: #111;
}
h1 { font-size: 20pt; margin: 0 0 6pt 0; }
.estimate { font-size: 10pt; color: #555; margin: 0 0 24pt 0; }
p { margin: 0 0 18pt 0; }
em { font-style: italic; }
"""


def _build_html(title: str, content: str, citations: list[dict]) -> str:
    spans = _find_quote_spans(content, citations)
    minutes = estimate_delivery_minutes(content)
    return (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        f"<style>{_CSS}</style></head><body>"
        f"<h1>{html.escape(title)}</h1>"
        f'<p class="estimate">~{minutes} min at an average pace — adjust to your own delivery.</p>'
        f"{_render_paragraphs_html(content, spans)}"
        "</body></html>"
    )


async def render_sermon_pdf(title: str, content: str, citations: list[dict]) -> bytes:
    """WeasyPrint's HTML.write_pdf() is synchronous/CPU-bound (real
    layout + rasterization work) — run_in_threadpool so it doesn't block
    the event loop, same pattern already used elsewhere in this codebase
    for other blocking calls (e.g. plan_limits.is_within_edit_cap)."""
    doc_html = _build_html(title, content, citations)
    return await run_in_threadpool(lambda: HTML(string=doc_html).write_pdf())
