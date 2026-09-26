import CallbackClient from './CallbackClient';

export const metadata = { title: 'Signing in' };
//: The query string carries a single-use code; nothing here is cacheable.
export const dynamic = 'force-dynamic';

/**
 * The URL registered as ARGUS's OIDC redirect URI.
 *
 * The identity provider sends the browser here, and this page hands the query
 * string to a client component that posts it to the API. Keeping the exchange
 * client-side is deliberate: the session secret must be written to the same
 * cookie the rest of the app reads, and that cookie belongs to the browser.
 *
 * `searchParams` is read on the server and passed down as a string rather than
 * read through `useSearchParams`, so there is no Suspense boundary to get
 * wrong and no static prerender of a page that is always dynamic.
 */
export default async function OidcCallbackPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (typeof value === 'string') query.set(key, value);
    else if (Array.isArray(value) && value.length > 0) query.set(key, value[0]);
  }
  return <CallbackClient search={query.toString()} />;
}
