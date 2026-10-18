# Web tests

- `e2e/`: Playwright end-to-end tests (launcher, settings, Notes, RSS, Review, Issues), run with
  `make test-e2e`. They start the server through Playwright's `webServer` against a temporary
  `JESINIA_HOME` and a fixture KB.
  - Review talks to a fake GitHub (`e2e/fixtures/github/server.py`) on an ephemeral loopback
    port, serving recorded-shape JSON for two PRs of `e2e-dev/greeter` and recording every
    request at `/__requests`. The server is pointed at it, and at the local bare repo the
    review workspace fetches from, only under the temporary-home guard in
    `jesinia.testing.fake_runner_app`; the real `gh` config and credentials are never read.
  - Issues uses the same fake GitHub: a test opens issues with `POST /__issues`
    (`{title, body, labels, parent}`), and the fake keeps labels, sub-issues, closes and comments in memory as
    GitHub would, refusing an unknown label rather than creating it. Its behaviour is unit-tested
    in `server/tests/testing/test_fixture_github.py`. The fake runner answers each Issues ask
    with an output valid against the schema it was asked for.
- Unit tests live next to each package and app in `src/__tests__/`.
