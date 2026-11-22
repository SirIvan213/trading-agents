// Vite + Vitest config for the read-only UI (UI track, CLAUDE.md).
// The page talks to nothing but its own API: in development /api is proxied to the local
// server (python -m ui.api, 127.0.0.1 only); the build is served by that same server.
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: { "/api": "http://127.0.0.1:8765" },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    assetsInlineLimit: 0, // fonts stay files: the CSP allows font-src 'self' only
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    unstubGlobals: true,
  },
});
