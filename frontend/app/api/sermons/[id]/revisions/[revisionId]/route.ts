// Proxies to the backend's GET /sermons/{id}/revisions/{revisionId}
// (Phase 8 Task 2) — revisionId is "current" or a revision UUID, both
// passed through untouched.
import { NextRequest, NextResponse } from "next/server";
import { backendFetch } from "@/lib/api-server";

type Params = { params: Promise<{ id: string; revisionId: string }> };

export async function GET(_req: NextRequest, { params }: Params) {
  const { id, revisionId } = await params;
  const resp = await backendFetch(`/sermons/${id}/revisions/${revisionId}`);
  if (resp === null) return NextResponse.json({ detail: "Not signed in" }, { status: 401 });
  const data = await resp.text();
  return new NextResponse(data, { status: resp.status, headers: { "Content-Type": "application/json" } });
}
