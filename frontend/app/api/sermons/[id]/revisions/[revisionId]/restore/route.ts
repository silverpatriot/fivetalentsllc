// Proxies to the backend's POST /sermons/{id}/revisions/{revisionId}/restore
// (Phase 8 Task 4).
import { NextRequest, NextResponse } from "next/server";
import { backendFetch } from "@/lib/api-server";

type Params = { params: Promise<{ id: string; revisionId: string }> };

export async function POST(_req: NextRequest, { params }: Params) {
  const { id, revisionId } = await params;
  const resp = await backendFetch(`/sermons/${id}/revisions/${revisionId}/restore`, { method: "POST" });
  if (resp === null) return NextResponse.json({ detail: "Not signed in" }, { status: 401 });
  const data = await resp.text();
  return new NextResponse(data, { status: resp.status, headers: { "Content-Type": "application/json" } });
}
