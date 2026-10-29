# Staff dashboard

A TypeScript + Vite single-page app for clinic staff, with no UI framework: plain DOM modules and small render helpers (`src/dom.ts`, `src/ui.ts`). It calls only the staff and admin endpoints of the Clinic Voice API (`/v1/staff/*`, `/v1/admin/*`). The backend authorizes every request; the dashboard only hides or disables controls by role.

Screens: sign-in, appointments (filters, calendar sync state, the needs-review queue with requeue or cancel, cancel, opt-out and remove opt-out), clinic settings (contact policy, scripts with owner approval, owner-only test calls), doctors, services and hours, schedule exceptions, reminders, outbox (dead jobs, requeue), calendar connection, provisioning, staff users (owner only) and my account (change password).

## Requirements

Node.js 20.19+ or 22.12+ and npm. Dependencies are dev-only and pinned exactly in `package.json` and `package-lock.json`: typescript, vite, vitest, openapi-typescript, jsdom (the DOM for tests) and @types/node.

## Setup

```sh
cd dashboard
npm ci
```

## Development

Run the API (README.md at the repository root), create a staff owner, then:

```sh
npm run dev   # http://127.0.0.1:5173
```

With `VITE_API_BASE_URL` empty (the default), the browser calls `/v1/...` on the dev server and Vite forwards it to `DASHBOARD_API_PROXY_TARGET` (default `http://127.0.0.1:8000`). The browser then sees one origin, so the API needs no CORS configuration. To call the API cross-origin instead, set `VITE_API_BASE_URL=http://127.0.0.1:8000` and start the API with `DASHBOARD_ORIGINS=http://127.0.0.1:5173`.

## Configuration

Variables are read from the environment or `dashboard/.env` (copy `.env.example`; `.env` is ignored by git):

| Variable | Used by | Meaning |
| --- | --- | --- |
| `VITE_API_BASE_URL` | build and dev | API origin, optionally with a path prefix, no trailing slash. https, or http only for localhost. Empty: same origin. It is compiled into the bundle and is not a secret. |
| `DASHBOARD_API_PROXY_TARGET` | dev server only | Where the Vite proxy forwards `/v1`. |

On the API side, `DASHBOARD_ORIGINS` lists the origins allowed to call it cross-origin (docs/OPERATIONS.md). Leave it empty when the dashboard is served on the API's origin.

## Build, typecheck and test

```sh
npm run typecheck   # tsc --noEmit, strict
npm run build       # dist/ (ignored by git)
npm test            # vitest run (jsdom)
```

The build injects a Content-Security-Policy meta tag (`vite.config.ts`): scripts, styles and images from the dashboard's own origin only, `connect-src` limited to the dashboard origin plus the `VITE_API_BASE_URL` origin, and Trusted Types enforced. Serve `dist/` with the response headers recommended in docs/SECURITY.md.

## API types

`src/api/schema.ts` is generated from `docs/openapi.json`. Do not edit it by hand. After a backend contract change, regenerate both:

```sh
.venv/bin/clinic-voice export-openapi > docs/openapi.json   # repository root
cd dashboard && npm run gen:api
```

`src/api/client.ts` derives each call's path, method, path and query parameters, body and response type from the generated `paths`, so `npm run typecheck` fails when the client no longer matches the contract. `tests/contract.test.ts` fails when `schema.ts` is stale.

## Security model

- The session token is held in memory only (`src/session.ts`). It is never written to localStorage, sessionStorage, cookies, the URL or logs. Reloading the page or opening a new tab signs out. Sign out calls `POST /v1/staff/logout`, and the session also ends at its `expires_at`.
- Every request sends `Authorization: Bearer <token>` with `credentials: "omit"`. A 401 on a request that carried a token clears the session and returns to sign-in.
- Error messages are mapped from the status (`src/api/errors.ts`). Only a short plain-text `detail` is shown, and only for 400, 409 and 422. Response bodies are never rendered.
- All data is rendered as Text nodes. No code assigns HTML strings to the DOM; a test scans the sources for HTML sinks, web storage and console logging.
- State codes become words only in `src/labels.ts`. "Confirmed" appears only for the exact appointment status `confirmed`; pending_sync shows "Pending calendar sync", needs_review shows "Needs review", and anything unrecognized shows "Unknown".
- Destructive or consequential actions ask for confirmation first.
- Calendar connection opens Google's consent page in a new tab (so this tab keeps its in-memory session). Only an https URL on `accounts.google.com` is accepted from the API.
