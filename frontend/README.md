# PowerMeter V2 frontend

React/TypeScript single-page application for the central PowerMeter V2 server. Browser traffic is same-origin and limited to `/api/v1`; the browser never communicates with a sensor or receives device credentials.

## Local verification

```powershell
npm ci
npm run check
npx playwright install chromium
npm run test:e2e
```

During development, set `PM_API_ORIGIN` to the local API origin before `npm run dev`. Production is served by the unprivileged Nginx image behind the product HTTPS gateway.

For a deterministic production-build demo, use two terminals. The fixture server contains authenticated PZEM-only electrical and committed-History evidence and is never bundled into the production application:

```powershell
npm run mock:api
```

```powershell
npm run build
npm run preview:test
```

Then open `http://127.0.0.1:4173`. Playwright also builds and exercises this production preview automatically; it does not take screenshots against the development server.

Live cards, History, energy, completeness, and cost surfaces are sourced from
independently accepted authenticated PZEM telemetry returned by the central
API. Utility PDFs are accepted only through the explicitly labeled rate-source
workflow; temporary browser data is cleared when review closes and no usage,
readings, bill totals, balances, payments, or customer identifiers are modeled.
