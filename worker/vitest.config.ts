import { cloudflareTest } from "@cloudflare/vitest-plugin";
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    projects: [
      {
        // Logic and CalDAV, in Node, against a real Radicale server.
        test: {
          name: "node",
          include: ["test/node/**/*.test.ts"],
          environment: "node",
          globalSetup: ["test/node/radicale.ts"],
          testTimeout: 20_000,
        },
      },
      {
        // OAuth, login page and MCP, inside workerd (the real Workers runtime).
        plugins: [
          cloudflareTest({
            wrangler: { configPath: "./wrangler.jsonc" },
            miniflare: {
              bindings: {
                CONNECTOR_PASSWORD: "correct horse battery",
                CALDAV_USERNAME: "test",
                CALDAV_PASSWORD: "test",
                BACKEND: "memory",
              },
            },
          }),
        ],
        test: { name: "worker", include: ["test/worker/**/*.test.ts"], testTimeout: 20_000 },
      },
    ],
  },
});
