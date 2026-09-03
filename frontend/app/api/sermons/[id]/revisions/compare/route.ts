// Proxies to the backend's GET /sermons/{id}/revisions/compare (Phase 8
// Task 3) — a literal "compare" segment, not a [revisionId], so it must
// stay a sibling file, never nested under the dynamic route directory
// (Next.js already resolves this literal route ahead of the dynamic
// one for an exact "/compare" match, same reasoning the backend route
// declaration order comment explains for its own two routes).
import { NextRequest, NextResponse } from "next/server";
import { backendFetch } from "@/lib/api-server";

type Params = { params: Promise<{ id: string }> };

export async function GET(req: NextRequest, { params }: Params) {
  const { id } = await params;
  const search = req.nextUrl.searchParams.toString();
  const resp = await backendFetch(`/sermons/${id}/revisions/compare?${search}`);
  if (resp === null) return NextResponse.json({ detail: "Not signed in" }, { status: 401 });
  const data = await resp.text();
  return new NextResponse(data, { status: resp.status, headers: { "Content-Type": "application/json" } });
}
