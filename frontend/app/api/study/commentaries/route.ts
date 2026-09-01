// Proxies to the backend's GET /study/commentaries — only commentaries
// actually ingested in this environment, not the full known-catalog list
// (see backend/app/api/study.py's list_commentaries docstring).
import { NextResponse } from "next/server";
import { backendFetch } from "@/lib/api-server";

export async function GET() {
  const resp = await backendFetch("/study/commentaries");
  if (resp === null) return NextResponse.json({ detail: "Not signed in" }, { status: 401 });
  const data = await resp.text();
  return new NextResponse(data, { status: resp.status, headers: { "Content-Type": "application/json" } });
}
