import { defineConfig } from "@playwright/test";

const port = 44173;
const baseURL = `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: "./tests/st",
  use: { baseURL, viewport: { width: 1600, height: 1080 } },
  webServer: {
    command: `npm run build && npm run preview -- --host 127.0.0.1 --port ${port} --strictPort`,
    url: baseURL,
  },
});
