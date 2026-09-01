// Proxies the backend's GET /media/{id}/audio — binary audio bytes, not
// JSON, so this forwards the body and Content-Type through as-is rather
// than assuming application/json the way the other proxy routes do.
import { NextRequest, NextResponse } from "next/server";
import { backendFetch } from "@/lib/api-server";

type Params = { params: Promise<{ id: string }> };

export async function GET(_req: NextRequest, { params }: Params) {
  const { id } = await params;
  const resp = await backendFetch(`/media/${id}/audio`);
  if (resp === null) return NextResponse.json({ detail: "Not signed in" }, { status: 401 });
  if (!resp.ok) {
    const data = await resp.text();
    return new NextResponse(data, { status: resp.status, headers: { "Content-Type": "application/json" } });
  }
  return new NextResponse(resp.body, {
    status: resp.status,
    headers: { "Content-Type": resp.headers.get("content-type") ?? "application/octet-stream" },
  });
}
