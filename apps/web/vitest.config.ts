import { defineConfig } from 'vitest/config';

/**
 * Unit-test configuration (hardening W3).
 *
 * Two runners live in this app and they must not collect each other's files:
 *
 * * **vitest** (`npm test`) — pure logic: presentation helpers, formatters, the
 *   announcement store. Fast, no browser, no network.
 * * **Playwright** (`npm run test:e2e`) — the browser gate in `e2e/`, which needs
 *   a running stack and a token.
 *
 * Vitest's default include pattern matches `*.spec.ts` anywhere, so the Playwright
 * spec was picked up by the unit runner — and failed there, for the unrelated
 * reason that no stack was running. Excluding `e2e/` is the fix; without it the
 * unit run reports a failure that has nothing to do with unit tests.
 */
export default defineConfig({
  test: {
    include: ['lib/**/*.test.ts', 'app/**/*.test.ts', 'app/**/*.test.tsx'],
    exclude: ['node_modules/**', '.next/**', 'e2e/**'],
  },
});
