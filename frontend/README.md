# frontend (M6: React + ElevenLabs client tools)

- `npm run dev`: Vite dev server on :5173; `/api/*` (including the WebSocket `/api/events`) is proxied to FastAPI on :8000
- `npm test`: Vitest unit / component tests; `npm run typecheck`
- `src/voice/tools.ts`: the 6 client tools (arguments come from the agent; the user's own words are attached by `UtteranceStore`)
- `src/voice/broadcast.ts`: backend events -> `sendUserMessage("[System update] ...")`, queued while the agent is speaking or a tool is running
- `src/voice/textRouter.ts`: direct text mode without the agent
- `src/panels/*`: mandate table, confirmation cards, positions, research top-up, guard alerts, bottom log

To start the whole stack, see `../docs/M6_voice_demo.md`.
