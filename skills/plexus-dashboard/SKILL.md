---
name: plexus-dashboard
description: Scaffold a working web dashboard against the Plexus telemetry API — device picker, latest-value tiles, time-series charts, log viewer. Use when the user wants to build a frontend, dashboard, ops UI, mission control, or fleet view on top of Plexus data. Triggering phrases include "show me my drones/satellites/robots", "build a dashboard for my fleet", "vibe code a Plexus frontend", "fleet monitoring UI", "telemetry dashboard", "device status page", "live charts of sensor data", or "ops view for my hardware" — even if "Plexus" is never said, as long as the data source is the Plexus API.
tools: Read, Write, Edit, Bash, WebFetch
---

# Plexus Dashboard

Scaffolds a complete web dashboard against the Plexus read API. This skill is opinionated about the stack so the user gets a working dashboard fast — they can swap pieces out later.

## When to use this skill

- The user is building a frontend / dashboard / ops UI on top of Plexus telemetry
- They mention "vibe code" + Plexus, or want to "build a dashboard for my fleet"
- They have a Plexus API key and want to _see_ their data, not just ingest it

If they need help with **ingesting** data (firmware / edge), use `plexus-firmware` instead.
For lower-level **API integration** without a UI, use the generic `plexus` skill.

## Preferred stack

Pick this unless the user explicitly wants something else:

- **Framework**: Next.js (App Router) — it's the default and Vercel-friendly
- **Styling**: Tailwind utilities only, no design system
- **Data fetching**: SWR (lighter than TanStack Query for this use case)
- **Charts**: Recharts (good defaults, easy to customize, no canvas)
- **Types**: TypeScript, strict

If the project is already React Router / Remix / Vite, adapt — don't force Next.js on top.

## Shape of the thing

```
Dashboard
├─ Source list (left rail) — /v1/sources, `online` + `last_seen_ms` are on each row
└─ Detail pane
   ├─ Tile row: every metric from /metrics/latest as a big-number card
   ├─ Chart grid: one line chart per metric over the last 1h via /metrics/query
   └─ (Optional) Log pane: /logs in a virtualized list
```

## Polling cadences

| Endpoint                          | Refresh interval |
| --------------------------------- | ---------------- |
| `/v1/sources`                     | 30s              |
| `/v1/sources/{id}/metrics/latest` | 5s               |
| `/v1/sources/{id}/metrics/query`  | 10s              |
| `/v1/fleet/health`                | 10s              |

Always use `refreshInterval` on SWR. Never use `setInterval` directly — SWR handles tab visibility, focus revalidation, and dedup.

**There is no per-source health endpoint.** `/v1/sources/{id}/health` 404s. Online status already rides along on `/v1/sources` — every row carries `online` and `last_seen_ms`, so a status dot needs no extra request. Don't poll for it.

Use `/v1/sources` (canonical). `/v1/devices` still works via a 307→308 redirect chain but is deprecated.

## Scaffolding workflow

When the user says "build me a Plexus dashboard":

### Step 1: Confirm scope

Ask exactly two questions, no more:

1. "Which sources? All of them, or one specific source_id?"
2. "Existing project to extend, or a fresh one?"

### Step 2: Set up the client

Create `src/lib/plexus.ts`:

```ts
const BASE = "https://plexus-data-api.fly.dev";
const KEY = process.env.NEXT_PUBLIC_PLEXUS_API_KEY!;

export class PlexusError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function get<T>(path: string): Promise<T> {
  const r = await fetch(`${BASE}${path}`, { headers: { "x-api-key": KEY } });
  if (!r.ok) throw new PlexusError(r.status, await r.text());
  return r.json();
}

/** Note: the path is `sources`, the response key is still `devices`. */
export type Source = { source_id: string; online: boolean; last_seen_ms: number | null };
export type Latest = { metrics: Record<string, number> };

/** The query response is COLUMNAR — parallel arrays, not an array of points. */
export type Series = {
  timestamp_ms: number[];
  min: number[];
  max: number[];
  avg: number[];
  count: number[];
};
export type Query = {
  interval: string;
  auto_downsampled: boolean;
  truncated: boolean;
  series: Record<string, Series>;
};

export const listSources = () => get<{ devices: Source[] }>("/v1/sources");
export const getLatest = (id: string) =>
  get<Latest>(`/v1/sources/${id}/metrics/latest`);
export const queryMetrics = (id: string, metrics: string[], last = "1h") =>
  get<Query>(
    `/v1/sources/${id}/metrics/query?metrics=${metrics.join(",")}&last=${last}`,
  );
export const fleetHealth = () =>
  get<{ sources_total: number; sources_online: number }>("/v1/fleet/health");

/** Zip the columnar response into what Recharts wants. */
export function toPoints(s: Series) {
  return s.timestamp_ms.map((t, i) => ({ t, v: s.avg[i] }));
}
```

### Step 3: Build the components

- `<SourceList>` — `useSWR("sources", listSources, { refreshInterval: 30_000 })`, renders `data.devices`
- `<StatusDot source>` — pure render off the `online` field already on the row. **No fetch.**
- `<LatestTiles sourceId>` — polls `/metrics/latest` every 5s, renders each entry as a card
- `<MetricChart sourceId metric range>` — `useSWR([id, metric, range], () => queryMetrics(id, [metric], range))`, then `toPoints(data.series[metric])` into a Recharts `<LineChart>`
- Surface `auto_downsampled` in the chart header ("1m buckets") so nobody misreads a smoothed line as raw data

### Step 4: Empty + error states

- If `/v1/sources` returns `{ devices: [] }`: show "No sources yet" with a copy-able curl pointing at `https://gateway.plexus.company/ingest`.
- On `PlexusError` with status 401: show "Check your PLEXUS_API_KEY".
- On other errors: log + small toast, don't blow up the UI.

### Step 5: Don't ship without

- Setting `NEXT_PUBLIC_PLEXUS_API_KEY` in `.env.local` (warn the user about exposure if their app is public)
- A README section with the env var + a one-line description
- One `npm run dev` smoke test before declaring done

## Upgrade path: drop polling, use the live stream

Once polling works end-to-end, swap `getLatest` polling for the WebSocket stream. Latest-value tiles update the moment a point is ingested instead of on the next 5s tick — feels dramatically better.

The stream is on the **data API**, not the gateway, and it **authenticates with its first message rather than a header** — which means a browser can connect directly, no relay needed:

```ts
export function subscribeMetrics(
  sourceId: string,
  metrics: string[],
  onPoints: (pts: Array<{ metric: string; value: number; timestamp: number }>) => void,
) {
  const qs = metrics.length ? `?metrics=${metrics.join(",")}` : "";
  const ws = new WebSocket(
    `wss://plexus-data-api.fly.dev/v1/sources/${sourceId}/metrics/stream${qs}`,
  );

  // Auth is the FIRST MESSAGE, not a header. Send it within 10s or the
  // server closes with 4401.
  ws.onopen = () => ws.send(JSON.stringify({ type: "auth", api_key: KEY }));

  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    // Frames are BATCHED: `points` is an array. Treating one frame as one
    // point silently drops data.
    if (m.type === "telemetry") onPoints(m.points);
    // m.type === "gateway_reconnecting" is informational; it resumes itself.
  };

  ws.onclose = (e) => {
    // 4401 = bad/missing key, 4402 = org access disabled. Neither is worth
    // retrying; anything else, reconnect with exponential backoff.
  };

  return () => ws.close();
}
```

No `pong` handling is needed on this endpoint — keepalive is at the protocol layer.

**Key exposure still applies.** Auth-by-message solves the browser's header limitation, not the "don't ship a secret to the public internet" problem. If the app is publicly reachable, put a relay in front and keep the key server-side.

Wire it alongside SWR: keep `useSWR` for initial load (UI hydrates with a value immediately) and let stream frames update the same cache via `mutate(key, fn, false)`. Charts can keep polling — querying a 1h window over WS is awkward and the 10s cadence is fine.

## What NOT to do

- Don't add auth/login flows. Tier_1 users have a single API key; this is meant to be embedded in their own auth-protected app.
- Don't add a backend layer "for safety" unless the user asks or the app is public. Direct browser → data API is the intended pattern for prototypes.
- Don't poll a per-source health endpoint. It doesn't exist.
- Don't type the query response as an array. It is columnar, and `.map(p => p.v)` over it yields `undefined` with no error — an empty chart and no clue why.
- Don't mock data. If the user has no sources yet, show the empty state with the ingest curl, not fake telemetry.
- Don't over-style. Tailwind utilities, gray scale, one accent color. The whole point is they can iterate on the design themselves.

## When unsure about endpoint shapes

`https://plexus-data-api.fly.dev/openapi.json` is the source of truth for HTTP. It does **not** list WebSocket routes — the generic `plexus` skill documents those.

Corrected 2026-08-28: `/v1/devices` → `/v1/sources`, removed the non-existent per-source health endpoint, fixed the columnar query type, and replaced the live-stream section (the old `wss://gateway.plexus.company/v1/stream` 404s, and the "browsers can't authenticate" caveat no longer holds).
