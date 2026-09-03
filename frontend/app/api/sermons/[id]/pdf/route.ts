// Proxies to the backend's GET /sermons/{id}/pdf. Unlike every other
// proxy route in this directory, the response body is binary (a real
// PDF) — passed through as a stream rather than read via resp.text(),
// which would corrupt it. Content-Disposition is forwarded as-is so the
// browser gets the backend's real filename, not a generic one.
import { NextRequest, NextResponse } from "next/server";
import { backendFetch } from "@/lib/api-server";

type Params = { params: Promise<{ id: string }> };

export async function GET(_req: NextRequest, { params }: Params) {
  const { id } = await params;
  const resp = await backendFetch(`/sermons/${id}/pdf`);
  if (resp === null) return NextResponse.json({ detail: "Not signed in" }, { status: 401 });
  if (!resp.ok) {
    const data = await resp.text();
    return new NextResponse(data, { status: resp.status, headers: { "Content-Type": "application/json" } });
  }
  const contentDisposition = resp.headers.get("Content-Disposition");
  return new NextResponse(resp.body, {
    status: resp.status,
    headers: {
      "Content-Type": "application/pdf",
      ...(contentDisposition ? { "Content-Disposition": contentDisposition } : {}),
    },
  });
}
