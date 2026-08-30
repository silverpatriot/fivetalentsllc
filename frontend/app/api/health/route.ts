// Proxies to FastAPI over the internal Docker network. The browser never
// talks to the backend directly — see docker-compose.yml's comment on why
// only `frontend` is exposed to the host.
import { NextResponse } from "next/server";

const BACKEND_URL = process.env.BACKEND_INTERNAL_URL ?? "http://backend:8000";

export async function GET() {
  try {
    const res = await fetch(`${BACKEND_URL}/health`, { cache: "no-store" });
    const body = await res.json();
    return NextResponse.json(body, { status: res.status });
  } catch {
    return NextResponse.json({ status: "unreachable" }, { status: 502 });
  }
}
