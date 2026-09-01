// Proxies to the backend's GET /concordance/search — query params
// forwarded as-is, same pattern as app/api/documents/route.ts's GET.
import { NextRequest, NextResponse } from "next/server";
import { backendFetch } from "@/lib/api-server";

export async function GET(req: NextRequest) {
  const params = req.nextUrl.searchParams;
  const resp = await backendFetch(`/concordance/search?${params.toString()}`);
  if (resp === null) return NextResponse.json({ detail: "Not signed in" }, { status: 401 });
  const data = await resp.text();
  return new NextResponse(data, { status: resp.status, headers: { "Content-Type": "application/json" } });
}
