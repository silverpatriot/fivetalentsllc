// Proxies to the backend's GET /bible/translations.
import { NextResponse } from "next/server";
import { backendFetch } from "@/lib/api-server";

export async function GET() {
  const resp = await backendFetch("/bible/translations");
  if (resp === null) return NextResponse.json({ detail: "Not signed in" }, { status: 401 });
  const data = await resp.text();
  return new NextResponse(data, { status: resp.status, headers: { "Content-Type": "application/json" } });
}
