import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import basicSsl from "@vitejs/plugin-basic-ssl";

// In dev, /api/* is proxied to the local FastAPI (including WebSocket /api/events): no CORS, no tunnels.
const BACKEND = process.env.VIBE_BACKEND ?? "http://127.0.0.1:8000";
// VIBE_LAN=1: listen on all interfaces so a phone on the same Wi-Fi can open it (scripts/dev.sh --lan, dev.ps1 -Lan).
// VIBE_HTTPS=1: self-signed HTTPS. Phones only allow the microphone on https:// (or localhost), so use it with LAN.
const LAN = process.env.VIBE_LAN === "1";
const HTTPS = process.env.VIBE_HTTPS === "1";

export default defineConfig({
  plugins: [react(), ...(HTTPS ? [basicSsl({ name: "vibe-trading-dev" })] : [])],
  server: {
    host: LAN ? "0.0.0.0" : "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": { target: BACKEND, changeOrigin: true, ws: true, rewrite: (p) => p.replace(/^\/api/, "") },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    globals: false,
  },
});
