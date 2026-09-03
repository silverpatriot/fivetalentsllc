import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Lean, self-contained production build for the Docker image.
  output: "standalone",
  experimental: {
    // 2026-09-03 real bug, confirmed live: middleware.ts's clerkMiddleware
    // runs on every route (including POST /api/media), and Next.js clones
    // + buffers the request body for middleware to read it, capped at a
    // DEFAULT of 10MB — silently. A real recording upload past 10MB (the
    // app's own advertised/enforced limit is 25MB — see backend/app/core/
    // config.py's max_media_upload_size_bytes) got truncated mid-multipart-
    // body before it ever reached app/api/media/route.ts's req.formData()
    // call, which then failed to parse the now-corrupt body and 500'd —
    // reproduced exactly: "Request body exceeded 10MB for /api/media" in
    // the frontend's own logs, immediately followed by "TypeError: Failed
    // to parse body as FormData." 30mb gives real headroom above the
    // app's 25MB limit for multipart boundary/header overhead, so a
    // genuinely-oversized upload hits the backend's own correctly-worded
    // rejection instead of a silent, confusing mid-flight truncation.
    middlewareClientMaxBodySize: "30mb",
  },
};

export default nextConfig;
