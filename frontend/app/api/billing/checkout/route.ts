// Proxies to the backend's POST /billing/checkout — the browser can't
// reach `backend:8000` directly (internal Docker network only, see
// docker-compose.yml), so every browser->backend call goes through a
// route handler like this one, same pattern as the Stripe/Clerk webhook
// proxies in app/api/webhooks/*.
import { NextRequest, NextResponse } from "next/server";
import { backendFetch } from "@/lib/api-server";

export async function POST(req: NextRequest) {
  const body = await req.text();
  const resp = await backendFetch("/billing/checkout", { method: "POST", body });
  if (resp === null) {
    return NextResponse.json({ detail: "Not signed in" }, { status: 401 });
  }
  const data = await resp.text();
  return new NextResponse(data, { status: resp.status, headers: { "Content-Type": "application/json" } });
}
