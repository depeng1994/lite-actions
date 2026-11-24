import { defineConfig } from "@playwright/test";

const port = 44173;
const baseURL = `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: "./tests/st",
  use: { baseURL, viewport: { width: 1600, height: 1080 } },
  webServer: {
    command: "node --experimental-strip-types tests/st/server.mjs",
    env: { ST_PORT: String(port) },
    gracefulShutdown: { signal: "SIGTERM", timeout: 1000 },
    url: baseURL,
  },
});
