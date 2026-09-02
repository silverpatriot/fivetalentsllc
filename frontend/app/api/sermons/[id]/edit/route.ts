// Phase 6: proxies to the backend's POST /sermons/{id}/edit, streaming
// its SSE response straight through unbuffered — same shape and same
// reasoning as ../generate/route.ts (progressive `delta` events need to
// actually arrive progressively on this side, not be held until the
// whole edit finishes).
import { NextRequest, NextResponse } from "next/server";
import { backendFetch } from "@/lib/api-server";

export const dynamic = "force-dynamic";

type Params = { params: Promise<{ id: string }> };

export async function POST(req: NextRequest, { params }: Params) {
  const { id } = await params;
  const body = await req.text();
  const resp = await backendFetch(`/sermons/${id}/edit`, { method: "POST", body });
  if (resp === null) return NextResponse.json({ detail: "Not signed in" }, { status: 401 });
  if (!resp.ok || resp.body === null) {
    const data = await resp.text();
    return new NextResponse(data, { status: resp.status, headers: { "Content-Type": "application/json" } });
  }
  return new NextResponse(resp.body, {
    status: resp.status,
    headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" },
  });
}
