import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

declare const process: { env: Record<string, string | undefined> };  // Node, without @types/node

// Security headers for the served page (a production web server should send the same; see README).
// Scripts only from this app; Google Fonts for the typefaces; inline style attributes (React) allowed.
const SECURITY_HEADERS = {
  "Content-Security-Policy": [
    "default-src 'self'", "script-src 'self'", "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com", "img-src 'self' data: blob:", "connect-src 'self'", "worker-src 'self' blob:",
    "object-src 'none'", "base-uri 'self'", "form-action 'self'", "frame-ancestors 'none'",
  ].join("; "),
  "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "same-origin",
  "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
};

export default defineConfig({
  plugins: [react()],
  // CRAFT_API points the dev proxy at another backend (e.g. a test copy); default is the local API server.
  server: { proxy: { "/api": process.env.CRAFT_API ?? "http://127.0.0.1:8001" } },
  // The fast production build (start_planner.ps1): same address and API as the dev server.
  preview: { host: "127.0.0.1", port: 5173, strictPort: true, proxy: { "/api": process.env.CRAFT_API ?? "http://127.0.0.1:8001" },
    headers: SECURITY_HEADERS },
  test: { environment: "jsdom", setupFiles: "./src/test-setup.ts", exclude: ["e2e/**", "node_modules/**", "legacy_demo/**"] },
});
