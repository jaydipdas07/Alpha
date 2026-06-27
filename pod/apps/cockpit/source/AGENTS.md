# Alpha Cockpit

This is a Lemma app app generated from the Vite template.

## Runtime

- App name: `cockpit`
- Pod id: `019ef606-7b77-76f1-853a-978ddf819415`
- API URL: `https://api.lemma.work`
- Auth URL: `https://lemma.work/auth`
- Served by host at the root of the app subdomain (base path `/`)

## Product Direction

- Build native React routes and components for the operator workflow.
- Replace sample routes, sample data, sidebar labels, fake users, placeholder metrics, and starter copy.
- Keep Lemma auth centralized through `AuthGuard`.
- Use `lemma-sdk/react` hooks and the shared `lemmaClient` from `src/lemma-client.ts`.
- Preferred style presets: neobrutal, editorial, soft, terminal.
- Current default style preset: `terminal`.
- Do not use Lemma registry scaffolding in this project.

## Calling the API

- Fetch once with hooks (`useRecords`, `useDatastoreQuery`, generated `use<Resource>List`). Never call `lemmaClient.records.list(...)` during render or in a `useEffect` with unstable deps — that loops.
- Realtime = subscribe, never poll. Use `useLiveRecords` for a live list (fetches once, then merges row deltas in place over the table WebSocket) or `useWatchChanges` for custom state. Never `setInterval(refetch)` — polling flickers and hammers the API.
- Don't flicker or reload: merge changes in place keyed by `record_id`, give list rows a stable `key` (the row id), and create the client once + gate auth once with `AuthGuard`.
- Writes through the generated CRUD hooks under `QueryClientProvider` auto-refresh the matching list — don't hand-wire a refetch.

## Verification

- Run the local dev server with the package manager used by the project.
- Run `npm run build`, `pnpm run build`, or `yarn run build` before deploy.
- Deploy with `lemma apps deploy cockpit --source-dir . --yes`.
