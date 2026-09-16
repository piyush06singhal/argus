import {
  api,
  type DependenciesHealth,
  type ServiceHealth,
} from '@/lib/api';

export const metadata = {
  title: 'Settings',
};

interface InfoRowProps {
  label: string;
  value: string;
}

function InfoRow({ label, value }: InfoRowProps) {
  return (
    <div className="flex items-center justify-between gap-4 py-3">
      <dt className="text-sm text-slate-400">{label}</dt>
      <dd className="font-mono text-sm text-slate-200">{value}</dd>
    </div>
  );
}

function DependencyRow({
  health,
}: {
  health: { name: string; status: string; latency_ms?: number | null; error?: string | null };
}) {
  const ok = health.status === 'healthy';
  return (
    <li className="flex items-center justify-between gap-4 py-3">
      <div className="flex items-center gap-2">
        <span
          className={`h-2 w-2 rounded-full ${
            ok ? 'bg-argus-success' : 'bg-argus-error'
          }`}
        />
        <span className="text-sm text-slate-200">{health.name}</span>
      </div>
      <div className="text-right">
        <span className={`text-sm ${ok ? 'text-argus-success' : 'text-argus-error'}`}>
          {ok ? 'healthy' : 'unhealthy'}
        </span>
        {ok && health.latency_ms != null && (
          <span className="ml-2 text-xs text-slate-500">
            {health.latency_ms.toFixed(1)} ms
          </span>
        )}
        {!ok && health.error && (
          <p className="mt-0.5 max-w-xs truncate text-xs text-argus-error">
            {health.error}
          </p>
        )}
      </div>
    </li>
  );
}

const API_ENDPOINTS: { method: string; path: string }[] = [
  { method: 'GET', path: '/health/live' },
  { method: 'GET', path: '/health/ready' },
  { method: 'GET', path: '/health/dependencies' },
  { method: 'GET/POST', path: '/api/v1/projects' },
  { method: 'GET/POST', path: '/api/v1/environments' },
  { method: 'GET/POST', path: '/api/v1/components' },
  { method: 'GET/POST', path: '/api/v1/dependencies' },
  { method: 'GET/POST', path: '/api/v1/incidents' },
  { method: 'GET/POST', path: '/api/v1/incidents/{id}/evidence' },
  { method: 'GET/POST', path: '/api/v1/deployments' },
  { method: 'GET/POST', path: '/api/v1/observability/events' },
  { method: 'GET/POST', path: '/api/v1/observability/logs' },
  { method: 'GET/POST', path: '/api/v1/observability/metrics' },
  { method: 'GET/POST', path: '/api/v1/observability/traces' },
];

export default async function SettingsPage() {
  let liveness: ServiceHealth | null = null;
  let dependencies: DependenciesHealth | null = null;
  let errorMessage: string | null = null;

  try {
    [liveness, dependencies] = await Promise.all([
      api.liveness(),
      api.dependencies(),
    ]);
  } catch (error) {
    errorMessage = error instanceof Error ? error.message : 'Unknown error';
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-100">Settings</h1>
        <p className="mt-1 text-sm text-slate-400">
          ARGUS platform configuration and service health
        </p>
      </div>

      {errorMessage && (
        <div className="card border-argus-error/40">
          <h2 className="font-medium text-argus-error">
            Failed to reach the ARGUS backend
          </h2>
          <p className="mt-2 text-sm text-slate-400">{errorMessage}</p>
        </div>
      )}

      <section className="card">
        <h2 className="font-medium text-slate-200">Platform</h2>
        <dl className="mt-2 divide-y divide-slate-800">
          <InfoRow label="Application" value="ARGUS Intelligence" />
          <InfoRow
            label="Version"
            value={liveness?.version ?? 'unavailable'}
          />
          <InfoRow
            label="Environment"
            value={liveness?.environment ?? 'unavailable'}
          />
          <InfoRow
            label="API status"
            value={liveness?.status ?? 'unreachable'}
          />
          <InfoRow
            label="Last checked"
            value={
              liveness?.timestamp
                ? new Date(liveness.timestamp).toLocaleString()
                : '—'
            }
          />
        </dl>
      </section>

      <section className="card">
        <h2 className="font-medium text-slate-200">Dependencies</h2>
        {dependencies && dependencies.dependencies.length > 0 ? (
          <ul className="mt-2 divide-y divide-slate-800">
            {dependencies.dependencies.map((dep) => (
              <DependencyRow key={dep.name} health={dep} />
            ))}
          </ul>
        ) : (
          <p className="mt-3 text-sm text-slate-400">
            {dependencies
              ? 'No dependency health reported.'
              : 'Dependency health unavailable.'}
          </p>
        )}
      </section>

      <section className="card">
        <h2 className="font-medium text-slate-200">API reference</h2>
        <ul className="mt-2 divide-y divide-slate-800">
          {API_ENDPOINTS.map((endpoint) => (
            <li
              key={endpoint.path}
              className="flex items-center justify-between gap-4 py-3"
            >
              <span className="rounded bg-slate-800 px-2 py-0.5 font-mono text-[11px] text-slate-400">
                {endpoint.method}
              </span>
              <span className="font-mono text-sm text-slate-200">
                {endpoint.path}
              </span>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}