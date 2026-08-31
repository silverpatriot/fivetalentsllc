// Proxies Stripe webhook deliveries to FastAPI over the internal Docker
// network. This exists because only `frontend` is exposed to the host
// (see docker-compose.yml) and Cloudflare Tunnel config is off-limits to
// change — this is the only path a third party (Stripe) can reach the
// backend through. Configure this route's public URL
// (https://<your-domain>/api/webhooks/stripe) as the endpoint in the
// Stripe Dashboard, not the backend's own /webhooks/stripe path directly.
//
// Forwards the raw request body byte-for-byte — Stripe's signature is
// computed over the exact bytes it sent; parsing and re-serializing as
// JSON would change them and break verification. The backend does the
// actual signature check; this route does no verification of its own.
import { NextResponse } from "next/server";

const BACKEND_URL = process.env.BACKEND_INTERNAL_URL ?? "http://backend:8000";

export async function POST(request: Request) {
  const rawBody = await request.arrayBuffer();
  const signature = request.headers.get("stripe-signature");
  if (!signature) {
    return NextResponse.json({ detail: "Missing Stripe-Signature header" }, { status: 400 });
  }

  try {
    const res = await fetch(`${BACKEND_URL}/webhooks/stripe`, {
      method: "POST",
      headers: { "content-type": "application/json", "stripe-signature": signature },
      body: rawBody,
      cache: "no-store",
    });
    const body = await res.json();
    return NextResponse.json(body, { status: res.status });
  } catch {
    return NextResponse.json({ detail: "backend unreachable" }, { status: 502 });
  }
}
