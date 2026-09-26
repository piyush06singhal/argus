'use client';

/**
 * Last-resort boundary (hardening W7).
 *
 * Reached when the root layout itself fails — so it must render its own
 * `<html>`/`<body>`. Deliberately dependency-free: no Tailwind classes that
 * depend on the app shell, no client data fetching. If ARGUS cannot render
 * even its chrome, the operator still needs to be told what broke.
 */
export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <html lang="en">
      <body
        style={{
          margin: 0,
          padding: '3rem 1.5rem',
          background: '#020617',
          color: '#e2e8f0',
          fontFamily:
            'ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif',
        }}
      >
        <main style={{ maxWidth: 640, margin: '0 auto' }}>
          <h1 style={{ fontSize: 20, margin: '0 0 8px' }}>
            ARGUS could not start this page
          </h1>
          <p style={{ color: '#94a3b8', fontSize: 14, margin: '0 0 16px' }}>
            This is the outermost error boundary — the failure happened before
            the application shell could render. Check the browser console and
            the web container log for the underlying error.
          </p>
          <pre
            style={{
              background: '#0f172a',
              border: '1px solid #1e293b',
              borderRadius: 6,
              padding: 12,
              fontSize: 12,
              overflowX: 'auto',
              margin: '0 0 16px',
            }}
          >
            {error.message}
            {error.digest ? `\n\ndigest: ${error.digest}` : ''}
          </pre>
          <button
            type="button"
            onClick={reset}
            style={{
              background: '#38bdf8',
              color: '#020617',
              border: 'none',
              borderRadius: 6,
              padding: '8px 16px',
              fontSize: 14,
              fontWeight: 500,
              cursor: 'pointer',
            }}
          >
            Try again
          </button>
        </main>
      </body>
    </html>
  );
}
