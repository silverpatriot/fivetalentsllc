// Proxies to the backend's GET /sermons/{id}/citations — same
// backendFetch pass-through shape as ../route.ts's GET. See that
// backend route's own docstring for why this exists (no persisted
// citation_flags column; the preach view needs real data on a cold load).
import { NextRequest, NextResponse } from "next/server";
import { backendFetch } from "@/lib/api-server";

type Params = { params: Promise<{ id: string }> };

export async function GET(_req: NextRequest, { params }: Params) {
  const { id } = await params;
  const resp = await backendFetch(`/sermons/${id}/citations`);
  if (resp === null) return NextResponse.json({ detail: "Not signed in" }, { status: 401 });
  const data = await resp.text();
  return new NextResponse(data, { status: resp.status, headers: { "Content-Type": "application/json" } });
}
