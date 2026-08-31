import { NextRequest, NextResponse } from "next/server";
import { backendFetch } from "@/lib/api-server";

export async function POST(req: NextRequest) {
  const body = await req.text();
  const resp = await backendFetch("/study/query", { method: "POST", body });
  if (resp === null) return NextResponse.json({ detail: "Not signed in" }, { status: 401 });
  const data = await resp.text();
  return new NextResponse(data, { status: resp.status, headers: { "Content-Type": "application/json" } });
}
