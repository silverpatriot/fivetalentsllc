// Proxies to the backend's POST /billing/activate-free — same pattern as
// app/api/billing/checkout/route.ts, but this path never touches Stripe
// (see that backend route's docstring): no line-items, just a bearer
// token forward.
import { NextRequest, NextResponse } from "next/server";
import { backendFetch } from "@/lib/api-server";

export async function POST(_req: NextRequest) {
  const resp = await backendFetch("/billing/activate-free", { method: "POST" });
  if (resp === null) {
    return NextResponse.json({ detail: "Not signed in" }, { status: 401 });
  }
  const data = await resp.text();
  return new NextResponse(data, { status: resp.status, headers: { "Content-Type": "application/json" } });
}
