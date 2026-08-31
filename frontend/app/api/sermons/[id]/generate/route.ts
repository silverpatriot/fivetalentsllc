// Streams the backend's Server-Sent-Events response straight through to
// the browser, unbuffered, so the progressive draft text (Task 3's
// streaming requirement) actually arrives progressively on this side too
// rather than being held until the whole generation finishes.
import { NextRequest, NextResponse } from "next/server";
import { backendFetch } from "@/lib/api-server";

export const dynamic = "force-dynamic";

type Params = { params: Promise<{ id: string }> };

export async function POST(req: NextRequest, { params }: Params) {
  const { id } = await params;
  const body = await req.text();
  const resp = await backendFetch(`/sermons/${id}/generate`, { method: "POST", body });
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
