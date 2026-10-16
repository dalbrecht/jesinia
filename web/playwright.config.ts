// SPDX-License-Identifier: AGPL-3.0-or-later
import { defineConfig } from "@playwright/test";
import { tmpdir } from "node:os";
import { join } from "node:path";

// One fixed scratch home per run (serve.sh wipes it): config is loaded in several
// processes, so it must not be random.
const home = join(tmpdir(), "jesinia-e2e");
process.env.E2E_HOME = home;
const PORT = 4875;

export default defineConfig({
  testDir: "tests/e2e",
  fullyParallel: false,
  workers: 1,
  use: { baseURL: `http://127.0.0.1:${PORT}`, trace: "retain-on-failure" },
  webServer: {
    command: "pnpm --filter @jesinia/launcher --filter @jesinia/notes build && bash tests/e2e/serve.sh",
    url: `http://127.0.0.1:${PORT}/healthz`,
    env: { E2E_HOME: home, E2E_PORT: String(PORT) },
    reuseExistingServer: false,
    timeout: 180_000,
    stdout: "pipe",
  },
});
