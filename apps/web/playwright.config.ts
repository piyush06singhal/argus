import { defineConfig, devices } from '@playwright/test';

/**
 * ARGUS browser gate configuration (hardening W3, G13).
 *
 * There is deliberately **no `webServer` block**: this suite runs against a real
 * stack (`docker compose up`), because the whole point is to exercise the app
 * the way an operator meets it — built production bundle, real API, real token —
 * rather than a `next dev` process that behaves differently (dev overlays,
 * unminified hydration, hot reload).
 *
 * Retries are zero by default so a flake is visible rather than absorbed, and
 * `CI=1` adds one retry plus the `github` reporter because a CI runner is a
 * noisier machine than the developer's laptop and a *single* retry there is the
 * difference between signal and noise, not a way of hiding a real failure.
 */
export default defineConfig({
  testDir: './e2e',
  // The suite is a smoke path, not a regression suite: a bounded timeout keeps
  // it honest about the claim "the UI loads quickly against a live stack".
  timeout: 30_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? [['list'], ['github']] : [['list']],
  use: {
    baseURL: process.env.ARGUS_WEB_URL ?? 'http://localhost:3000',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    // The token cookie is not httpOnly (the browser must attach it as a header),
    // so the suite can set it exactly the way the connect form does.
    ...devices['Desktop Chrome'],
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
