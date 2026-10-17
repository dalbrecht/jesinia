// SPDX-License-Identifier: AGPL-3.0-or-later
import { defineConfig } from "@playwright/test";
import { tmpdir } from "node:os";
import { join } from "node:path";

// One fixed scratch home per run (fixture.sh wipes it): config is loaded in several
// processes, so it must not be random.
const home = join(tmpdir(), "jesinia-e2e");
process.env.E2E_HOME = home;
const PORT = 4875;
// The fixture feeds' own loopback port (`make e2e-server`); the fixtures' URLs name it.
const FEEDS_PORT = 4876;
process.env.E2E_FEEDS_PORT = String(FEEDS_PORT);

export default defineConfig({
  testDir: "tests/e2e",
  fullyParallel: false,
  workers: 1,
  use: { baseURL: `http://127.0.0.1:${PORT}`, trace: "retain-on-failure" },
  webServer: {
    command: "make -C .. e2e-server",
    url: `http://127.0.0.1:${PORT}/healthz`,
    env: { E2E_HOME: home, E2E_PORT: String(PORT), E2E_FEEDS_PORT: String(FEEDS_PORT) },
    reuseExistingServer: false,
    timeout: 180_000,
    stdout: "pipe",
  },
});
