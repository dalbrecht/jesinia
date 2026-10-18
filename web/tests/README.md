# Web tests

- `e2e/`: Playwright end-to-end tests (launcher, settings, Notes, RSS, Review), run with
  `make test-e2e`. They start the server through Playwright's `webServer` against a temporary
  `JESINIA_HOME` and a fixture KB.
  - Review talks to a fake GitHub (`e2e/fixtures/github/server.py`) on an ephemeral loopback
    port, serving recorded-shape JSON for two PRs of `e2e-dev/greeter` and recording every
    request at `/__requests`. The server is pointed at it, and at the local bare repo the
    review workspace fetches from, only under the temporary-home guard in
    `jesinia.testing.fake_runner_app`; the real `gh` config and credentials are never read.
- Unit tests live next to each package and app in `src/__tests__/`.
