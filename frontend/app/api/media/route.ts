// Proxies to the backend's /media (app/api/media.py). POST forwards the
// browser's multipart FormData through untouched — see lib/api-server.ts's
// backendFetch for why it must NOT set a Content-Type header itself. Same
// shape as app/api/documents/route.ts.
import { NextRequest, NextResponse } from "next/server";
import { backendFetch } from "@/lib/api-server";

export async function GET(req: NextRequest) {
  const sermonId = req.nextUrl.searchParams.get("sermon_id");
  const path = sermonId ? `/media?sermon_id=${encodeURIComponent(sermonId)}` : "/media";
  const resp = await backendFetch(path);
  if (resp === null) return NextResponse.json({ detail: "Not signed in" }, { status: 401 });
  const data = await resp.text();
  return new NextResponse(data, { status: resp.status, headers: { "Content-Type": "application/json" } });
}

export async function POST(req: NextRequest) {
  const formData = await req.formData();
  const resp = await backendFetch("/media", { method: "POST", body: formData });
  if (resp === null) return NextResponse.json({ detail: "Not signed in" }, { status: 401 });
  const data = await resp.text();
  return new NextResponse(data, { status: resp.status, headers: { "Content-Type": "application/json" } });
}
