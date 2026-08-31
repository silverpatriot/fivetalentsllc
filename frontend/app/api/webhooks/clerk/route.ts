// Proxies Clerk webhook deliveries to FastAPI — same reasoning as
// app/api/webhooks/stripe/route.ts. Configure this route's public URL
// (https://<your-domain>/api/webhooks/clerk) as the endpoint in the
// Clerk Dashboard.
//
// Forwards the raw body and all three Svix headers byte-for-byte/exactly
// — Svix's signature covers the exact bytes and the svix-id/timestamp
// values; this route does no verification of its own, the backend does.
import { NextResponse } from "next/server";

const BACKEND_URL = process.env.BACKEND_INTERNAL_URL ?? "http://backend:8000";

export async function POST(request: Request) {
  const rawBody = await request.arrayBuffer();
  const svixId = request.headers.get("svix-id");
  const svixTimestamp = request.headers.get("svix-timestamp");
  const svixSignature = request.headers.get("svix-signature");
  if (!svixId || !svixTimestamp || !svixSignature) {
    return NextResponse.json({ detail: "Missing Svix headers" }, { status: 400 });
  }

  try {
    const res = await fetch(`${BACKEND_URL}/webhooks/clerk`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "svix-id": svixId,
        "svix-timestamp": svixTimestamp,
        "svix-signature": svixSignature,
      },
      body: rawBody,
      cache: "no-store",
    });
    const body = await res.json();
    return NextResponse.json(body, { status: res.status });
  } catch {
    return NextResponse.json({ detail: "backend unreachable" }, { status: 502 });
  }
}
