/**
 * ARGUS API — the server-aware entry point (hardening W7).
 *
 * Server components and server-side route handlers import from here. It exists
 * for one reason: a server-rendered page must fetch as *its caller*, using the
 * token on the incoming request, rather than as some shared service identity.
 * That property is what stops a project-scoped VIEWER from being shown an
 * administrator's view simply because the render happened on the server.
 *
 * The client itself — and every type — lives in ``lib/api-client.ts``, which is
 * safe to import from a browser boundary. This module re-exports all of it, so
 * server code keeps its existing imports, and registers the request-scoped
 * resolver that the underlying client calls whenever it needs a token and no
 * ``window`` is present. Browser-boundary components must import
 * ``lib/api-client.ts`` directly: this module imports ``next/headers`` through
 * ``server-token`` and cannot be part of the client bundle.
 */

import { getServerToken } from './server-token';
import { registerRequestTokenResolver } from './api-client';

//: Registered once per server process. The resolver holds no request data: it
//: reads Next's per-request cookie store each time it runs.
registerRequestTokenResolver(async () => {
  try {
    return await getServerToken();
  } catch {
    // Outside a request scope (build, static generation) there is no cookie
    // store; the client then falls back to ARGUS_API_TOKEN.
    return null;
  }
});

export * from './api-client';
