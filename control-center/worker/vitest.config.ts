import { cloudflareTest } from "@cloudflare/vitest-pool-workers";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [cloudflareTest({ wrangler: { configPath: "./wrangler.toml" } })],
  test: {
    include: ["tests/**/*.test.ts"],
    exclude: ["tests/portability-intercept.test.ts", "tests/node-sqlite-adapter.test.mjs"],
  },
});
