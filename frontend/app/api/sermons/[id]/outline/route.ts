// Proxies to the backend's POST /sermons/{id}/outline — same backendFetch
// pass-through shape as ../route.ts's PATCH.
import { NextRequest, NextResponse } from "next/server";
import { backendFetch } from "@/lib/api-server";

type Params = { params: Promise<{ id: string }> };

export async function POST(req: NextRequest, { params }: Params) {
  const { id } = await params;
  const body = await req.text();
  // No explicit Content-Type header — backendFetch (lib/api-server.ts)
  // sets application/json automatically for a non-FormData body when
  // none is already present, same as ../route.ts's PATCH handler above.
  const resp = await backendFetch(`/sermons/${id}/outline`, { method: "POST", body });
  if (resp === null) return NextResponse.json({ detail: "Not signed in" }, { status: 401 });
  const data = await resp.text();
  return new NextResponse(data, { status: resp.status, headers: { "Content-Type": "application/json" } });
}
