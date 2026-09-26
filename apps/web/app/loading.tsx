/**
 * Global loading boundary (hardening W7).
 *
 * What this replaces: a blank page. Next.js streams the shell immediately and
 * then waits for the server component's data, so without a boundary a slow
 * query looks like a broken app — the worst possible impression on a first run,
 * when the API is still building its first baselines.
 *
 * Why a skeleton rather than a spinner: the layout is stable across every page
 * in ARGUS (a title, a summary strip, a table), so showing that shape tells the
 * user what is coming and stops the page from jumping when it arrives. A
 * spinner only says "something is happening", which they already knew.
 */
export default function Loading() {
  return (
    <div className="space-y-6 p-6" aria-busy="true" aria-live="polite">
      <span className="sr-only">Loading ARGUS data…</span>

      <div className="h-7 w-56 animate-pulse rounded bg-slate-800" />

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        {[0, 1, 2, 3].map((index) => (
          <div
            key={index}
            className="card space-y-2"
            data-testid="loading-tile"
          >
            <div className="h-3 w-24 animate-pulse rounded bg-slate-800" />
            <div className="h-6 w-16 animate-pulse rounded bg-slate-800" />
          </div>
        ))}
      </div>

      <div className="card space-y-3">
        {[0, 1, 2, 3, 4].map((index) => (
          <div key={index} className="flex items-center gap-3">
            <div className="h-4 w-40 animate-pulse rounded bg-slate-800" />
            <div className="h-4 flex-1 animate-pulse rounded bg-slate-800" />
            <div className="h-4 w-20 animate-pulse rounded bg-slate-800" />
          </div>
        ))}
      </div>
    </div>
  );
}
