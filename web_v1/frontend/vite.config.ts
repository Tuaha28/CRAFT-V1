import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

declare const process: { env: Record<string, string | undefined> };  // Node, without @types/node

export default defineConfig({
  plugins: [react()],
  // CRAFT_API points the dev proxy at another backend (e.g. a test copy); default is the local API server.
  server: { proxy: { "/api": process.env.CRAFT_API ?? "http://127.0.0.1:8001" } },
  // The fast production build (start_planner.ps1): same address and API as the dev server.
  preview: { host: "127.0.0.1", port: 5173, strictPort: true, proxy: { "/api": process.env.CRAFT_API ?? "http://127.0.0.1:8001" } },
  test: { environment: "jsdom", setupFiles: "./src/test-setup.ts", exclude: ["e2e/**", "node_modules/**", "legacy_demo/**"] },
});
