// Client-side download only — no PDF-generation dependency exists
// anywhere in this stack (pypdf, already a backend dependency, is only
// ever used for READING uploaded PDFs during document ingestion, see
// backend/app/services/extraction.py — confirmed, not something this can
// reuse for generation). A Blob + <a download> of plain text/markdown is
// the standard, zero-new-dependency approach; a print-friendly view
// (@media print, letting the browser's own print-to-PDF handle real PDF
// output) is the pragmatic fallback if that's ever wanted later — not
// built here.
export function downloadText(filename: string, text: string, mime = "text/markdown") {
  const blob = new Blob([text], { type: `${mime};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
