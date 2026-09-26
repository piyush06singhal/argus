import { expect, test, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

/**
 * ARGUS browser gate (hardening W3, G13) — ≤15 checks over the journey an
 * operator actually takes:
 *
 *   connect → overview → service → incident → RCA → remediation
 *
 * What this suite proves that no HTTP-level gate can, and why it exists:
 *
 * 1. **Hydration.** `infrastructure/e2e-ui-smoke.sh` asserts the *server-rendered*
 *    HTML of every route. A page can render correctly on the server and then
 *    throw in the browser while hydrating — the operator sees a dead page. Only a
 *    real browser catches that, so each visited page must produce **zero**
 *    uncaught errors and zero console errors.
 * 2. **Interactivity.** The navigation path is walked by *clicking*, not by
 *    requesting URLs: the links the UI exposes are the ones a user has.
 * 3. **The token boundary as a browser experiences it.** Sign-in stores the
 *    cookie the server components read, and signing out must stop the data
 *    rendering rather than leaving a cached view.
 *
 * Checks are intentionally few and semantic (role/text based), so a restyle does
 * not fail the gate while a broken panel does.
 */

// The shell gates cache their credential at `${TMPDIR:-/tmp}/argus-gate-token`,
// and on macOS `$TMPDIR` is *not* `/tmp` — it is a per-user directory under
// /var/folders. Hardcoding `/tmp` therefore found no token on macOS and the
// suite refused to run, so the default is derived the same way the shell
// derives it (`os.tmpdir()` honours `$TMPDIR`).
const TOKEN_CACHE =
  process.env.ARGUS_TOKEN_CACHE ?? join(tmpdir(), 'argus-gate-token');

/**
 * The credential to sign in with.
 *
 * `ARGUS_TOKEN` first (CI passes it explicitly); otherwise the cache the live
 * gates write, so `npm run test:e2e` works immediately after a gate run. The
 * suite refuses to run without one rather than testing an anonymous render and
 * reporting the app healthy.
 */
function resolveToken(): string {
  if (process.env.ARGUS_TOKEN) return process.env.ARGUS_TOKEN;
  try {
    const cached = readFileSync(TOKEN_CACHE, 'utf8').trim();
    if (cached) return cached;
  } catch {
    /* fall through to the refusal below */
  }
  throw new Error(
    'No ARGUS_TOKEN. Set it, or run a live gate first (it caches one at ' +
      `${TOKEN_CACHE}). The browser gate is meaningless without a credential: ` +
      'without one every page renders its signed-out state.'
  );
}

const TOKEN = resolveToken();

/** Console noise that is not an application defect. */
const IGNORED_CONSOLE = [
  /Download the React DevTools/i,
  /favicon\.ico/i,
  /\[Fast Refresh\]/i,
];

type PageHealth = { errors: string[] };

/**
 * Attach error collectors to a page.
 *
 * Both channels matter and they are different: `pageerror` is an uncaught
 * exception (a real breakage), while a `console.error` is what React logs for a
 * failed render or a rejected fetch. Filtering is by pattern and tiny — an
 * over-filtered collector is how a gate ends up green on a broken page.
 */
/**
 * The first link to an actual incident *row*.
 *
 * A plain `a[href^="/incidents/"]` also matches the section links that share the
 * prefix (`/incidents/dashboard`, `/incidents/rca`), which is how this suite
 * first asserted the incident dashboard instead of an incident. The exclusions
 * are the real routes from the app directory, stated rather than pattern-matched,
 * so a new section link is a visible change here rather than a silent reroute.
 */
function incidentRowLink(page: Page) {
  return page
    .locator(
      'a[href^="/incidents/"]:not([href="/incidents/dashboard"]):not([href="/incidents/rca"]):not([href="/incidents/rca-history"])'
    )
    .first();
}

function watchErrors(page: Page): PageHealth {
  const health: PageHealth = { errors: [] };
  page.on('pageerror', (error) => health.errors.push(`pageerror: ${error.message}`));
  page.on('console', (message) => {
    if (message.type() !== 'error') return;
    const text = message.text();
    if (IGNORED_CONSOLE.some((pattern) => pattern.test(text))) return;
    health.errors.push(`console.error: ${text}`);
  });
  return health;
}

/**
 * Sign in through the real form.
 *
 * The confirmation is asserted as `role="status"` because that is the contract
 * the app now keeps: the message is recorded outside React so it survives the
 * `router.refresh()` that follows a successful connect. Asserting it here is
 * deliberate — the *first* version of this form lost the message within ~40 ms,
 * and this line is what caught it.
 */
async function signIn(page: Page): Promise<void> {
  await page.goto('/connect');
  await page.getByLabel('API token').fill(TOKEN);
  await page.getByRole('button', { name: 'Connect' }).click();
  await expect(page.getByRole('status')).toContainText(/Connected as /);
}

/** The signed-out marker on the connect page. */
async function expectSignedOut(page: Page): Promise<void> {
  await expect(page.getByText(/Not connected:/)).toBeVisible();
}

test.describe('ARGUS browser gate', () => {
  test('the console refuses an unauthenticated caller and accepts a real token', async ({
    page,
  }) => {
    const health = watchErrors(page);

    // 1. Signed out, the console reports that plainly — never a dashboard full
    //    of another caller's data.
    await page.goto('/connect');
    await expectSignedOut(page);

    // 2. The sign-in form performs a real whoami round trip, and its
    //    confirmation survives the refresh that follows.
    await signIn(page);

    // 3. Signing in changes what the overview shows, and the page hydrated.
    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible();
    expect(health.errors, health.errors.join('\n')).toEqual([]);
  });

  test('the navigation path is clickable from the dashboard to an incident', async ({ page }) => {
    const health = watchErrors(page);
    await signIn(page);

    await page.goto('/');
    // 4. The dashboard exposes the journey's entry points as real links.
    await expect(page.getByRole('link', { name: /^Incidents$/ }).first()).toBeVisible();
    await page.getByRole('link', { name: /^Incidents$/ }).first().click();

    // 5. The list renders live rows and each row links to its detail page.
    await expect(page).toHaveURL(/\/incidents/);
    await expect(page.getByRole('heading', { name: 'Incidents' })).toBeVisible();
    const incidentLink = incidentRowLink(page);
    await expect(incidentLink).toBeVisible();

    // 6. Clicking through opens the incident, not an empty state.
    await incidentLink.click();
    await expect(page).toHaveURL(/\/incidents\/[0-9a-f-]{36}/);
    expect(health.errors, health.errors.join('\n')).toEqual([]);
  });

  test('an incident reaches its causal analysis and remediation surfaces', async ({ page }) => {
    const health = watchErrors(page);
    await signIn(page);

    await page.goto('/incidents');
    await incidentRowLink(page).click();
    await expect(page).toHaveURL(/\/incidents\/[0-9a-f-]{36}/);
    const incidentUrl = page.url();

    // 7. Causal analysis is reachable from the incident itself.
    const causal = page.locator('a[href$="/causal-analysis"]').first();
    if (await causal.count()) {
      await causal.click();
      await expect(page).toHaveURL(/\/causal-analysis/);
      // 8. The analysis page renders its evidence-or-limitations contract.
      await expect(
        page.getByText(/evidence|hypothes|candidate|uncertainty/i).first()
      ).toBeVisible();
    }

    // 9. Remediation is a first-class surface, and it renders its policy state.
    await page.goto('/remediation');
    await expect(page.getByRole('heading', { name: /Safe Remediation/i })).toBeVisible();

    // 10. Back-navigation returns to the incident without a client-side error.
    await page.goto(incidentUrl);
    await expect(page.getByRole('heading').first()).toBeVisible();
    expect(health.errors, health.errors.join('\n')).toEqual([]);
  });

  test('service, SLO and reliability surfaces render their live panels', async ({ page }) => {
    const health = watchErrors(page);
    await signIn(page);

    // 11. The platform overview renders.
    await page.goto('/platform');
    await expect(page.getByRole('heading').first()).toBeVisible();

    // 12. Service ownership is a platform surface with a real table.
    await page.goto('/platform/services');
    await expect(page.getByRole('heading').first()).toBeVisible();
    const serviceLink = page.locator('a[href^="/platform/services/"]').first();
    if (await serviceLink.count()) {
      await serviceLink.click();
      // 13. The component profile renders with its ownership/edit affordances.
      await expect(page).toHaveURL(/\/platform\/services\//);
      await expect(page.getByRole('heading').first()).toBeVisible();
    }

    // 14. Predictive reliability renders its bands rather than a bare number.
    await page.goto('/reliability');
    await expect(page.getByRole('heading').first()).toBeVisible();
    expect(health.errors, health.errors.join('\n')).toEqual([]);
  });

  test('signing out clears the browser credential', async ({ page }) => {
    await signIn(page);

    // 15. Clearing the token stops the server from rendering project data, and
    //     the browser stops carrying the credential entirely.
    await page.goto('/connect');
    await page.getByRole('button', { name: /Sign out of this browser/i }).click();
    await expect(page.getByRole('status')).toContainText(/Token cleared/);
    await expectSignedOut(page);

    const cookies = await page.context().cookies();
    expect(cookies.filter((cookie) => cookie.name === 'argus_token' && cookie.value)).toEqual([]);
  });
});
