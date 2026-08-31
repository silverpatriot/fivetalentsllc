import { NextRequest, NextResponse } from "next/server";
import { backendFetch } from "@/lib/api-server";

type Params = { params: Promise<{ id: string }> };

export async function DELETE(_req: NextRequest, { params }: Params) {
  const { id } = await params;
  const resp = await backendFetch(`/documents/${id}`, { method: "DELETE" });
  if (resp === null) return NextResponse.json({ detail: "Not signed in" }, { status: 401 });
  if (resp.status === 204) return new NextResponse(null, { status: 204 });
  const data = await resp.text();
  return new NextResponse(data, { status: resp.status, headers: { "Content-Type": "application/json" } });
}
