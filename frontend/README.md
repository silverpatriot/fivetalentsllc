# Sermon Engine — Frontend

Next.js 15 (App Router) + React 19 + Tailwind CSS v4 + shadcn/ui.

## Development

```bash
npm install
npm run dev
```

Talks to the backend over the internal Docker network in production
(`BACKEND_INTERNAL_URL`, default `http://backend:8000`) — see
`app/api/health/route.ts` for the pattern: browser code calls a Next.js
route handler, which calls FastAPI server-side. The browser never calls
FastAPI directly (see the root `docker-compose.yml` for why).

## Adding shadcn/ui components

```bash
npx shadcn@latest add <component>
```
