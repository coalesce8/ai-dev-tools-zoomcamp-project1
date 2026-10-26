import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Read only by the dev server (`npm run dev`). The built app always calls the
// relative path /api/v1; whatever serves it in Compose or k8s does the proxying.
const apiProxyTarget = process.env.API_PROXY_TARGET || "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      // Only the public API. /internal/v1 and /healthz stay off the proxy.
      "/api/v1": {
        target: apiProxyTarget,
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
