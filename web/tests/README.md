# Web tests

- `e2e/`: Playwright end-to-end tests (launcher, settings, Notes), run with `make test-e2e`.
  They start the server through Playwright's `webServer` against a temporary `JESINIA_HOME`
  and a fixture KB.
- Unit tests live next to each package and app in `src/__tests__/`.
