// Proxies to the backend's /documents (Phase 4 Task 1's shared upload
// pipeline — Task 3 reuses it as-is for the theology corpus, no new
// backend upload endpoint needed). POST forwards the browser's
// multipart FormData through untouched — see lib/api-server.ts's
// backendFetch for why it must NOT set a Content-Type header itself.
import { NextRequest, NextResponse } from "next/server";
import { backendFetch } from "@/lib/api-server";

export async function GET(req: NextRequest) {
  const corpusType = req.nextUrl.searchParams.get("corpus_type");
  const path = corpusType ? `/documents?corpus_type=${encodeURIComponent(corpusType)}` : "/documents";
  const resp = await backendFetch(path);
  if (resp === null) return NextResponse.json({ detail: "Not signed in" }, { status: 401 });
  const data = await resp.text();
  return new NextResponse(data, { status: resp.status, headers: { "Content-Type": "application/json" } });
}

export async function POST(req: NextRequest) {
  const formData = await req.formData();
  const resp = await backendFetch("/documents", { method: "POST", body: formData });
  if (resp === null) return NextResponse.json({ detail: "Not signed in" }, { status: 401 });
  const data = await resp.text();
  return new NextResponse(data, { status: resp.status, headers: { "Content-Type": "application/json" } });
}
