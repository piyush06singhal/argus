import { api, formatDate, type DependencyHealthResponse, type PlatformHealthResponse } from '@/lib/api';
import {
  PLATFORM_BOUNDARY,
  subsystemRequirementLabel,
  subsystemStatusLabel,
  subsystemStatusStyle,
} from '@/lib/platform';
import { Card, Empty, Facts, Limitations, Row, Table, Tile, readError } from '../ui';

export const metadata = {
  title: 'Platform Health',
};

export const dynamic = 'force-dynamic';

/**
 * ARGUS self-monitoring (§57–§60, §105–§107).
 *
 * The rule: **readiness is about required subsystems only.** An optional
 * integration being down (an AI provider, for instance) degrades a capability
 * and is shown as such; it does not render the platform unready, because a
 * system that refuses traffic when its optional AI is offline is less reliable
 * than one that loses a feature. Required and optional are labelled on every
 * row.
 */
export default async function PlatformHealthPage() {
  let health: PlatformHealthResponse | null = null;
  let dependencies: DependencyHealthResponse | null = null;
  let error: string | null = null;

  const [healthResult, depsResult] = await Promise.allSettled([
    api.platformHealth(),
    api.platformDependencies(),
  ]);
  if (healthResult.status === 'fulfilled') {
    health = healthResult.value;
  } else {
    error = readError(healthResult.reason);
  }
  if (depsResult.status === 'fulfilled') {
    dependencies = depsResult.value;
  }

  if (error || !health) {
    return (
      <Card title="Platform health unavailable">
        <Empty>{error ?? 'ARGUS could not read its own health.'}</Empty>
      </Card>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-100">Platform Health</h1>
        <p className="mt-1 text-sm text-slate-400">
          ARGUS watching itself: subsystems, dependencies and graceful
          degradation. A degraded optional capability is named, not hidden.
        </p>
        <p className="mt-2 text-xs text-slate-500">{PLATFORM_BOUNDARY}</p>
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Tile
          label="Status"
          value={health.status}
          hint={`As of ${formatDate(health.as_of)}`}
        />
        <Tile
          label="Ready"
          value={health.ready ? 'Yes' : 'No'}
          hint="Required subsystems only"
        />
        <Tile
          label="Subsystems"
          value={health.subsystems.length}
          hint={`${health.summary.OK ?? 0} OK`}
        />
        <Tile
          label="Degraded capabilities"
          value={health.degraded_capabilities.length}
          hint={
            health.degraded_capabilities.length === 0
              ? 'None — every capability is available'
              : health.degraded_capabilities.join(', ')
          }
        />
      </div>

      {health.degraded_capabilities.length > 0 ? (
        <Card
          title="Degraded capabilities"
          subtitle="§59 — the platform continues without them"
        >
          <ul className="space-y-1 text-sm text-slate-300">
            {health.degraded_capabilities.map((item) => (
              <li key={item}>· {item}</li>
            ))}
          </ul>
        </Card>
      ) : null}

      <Card title="Subsystems" subtitle="§58 — status, latency and last success/failure">
        <Table
          headers={['Subsystem', 'Status', 'Role', 'Latency', 'Queue', 'Last success', 'Last failure']}
          rows={health.subsystems.map((item) => [
            item.name,
            <span key="s" className={`badge ${subsystemStatusStyle(item.status)}`}>
              {subsystemStatusLabel(item.status)}
            </span>,
            subsystemRequirementLabel(item.required),
            item.latency_ms != null ? `${Math.round(item.latency_ms)} ms` : '—',
            item.queue_depth != null ? String(item.queue_depth) : '—',
            formatDate(item.last_success_at),
            formatDate(item.last_failure_at),
          ])}
        />
        <Limitations items={health.notes} />
      </Card>

      {dependencies ? (
        <Card
          title="Dependencies & graceful degradation"
          subtitle="§59, §60 — what fails safe and what does not"
        >
          <div className="grid gap-4 lg:grid-cols-2">
            <div>
              <h3 className="mb-2 text-xs uppercase tracking-wider text-slate-500">
                Dependency health
              </h3>
              <Table
                headers={['Dependency', 'Status', 'Required']}
                rows={dependencies.dependencies.map((item) => [
                  item.name,
                  <span key="s" className={`badge ${subsystemStatusStyle(item.status)}`}>
                    {subsystemStatusLabel(item.status)}
                  </span>,
                  item.required ? 'Yes' : 'No',
                ])}
              />
            </div>
            <div>
              <h3 className="mb-2 text-xs uppercase tracking-wider text-slate-500">
                Graceful degradation
              </h3>
              {Object.entries(dependencies.graceful_degradation).map(([key, value]) => (
                <Row key={key} label={key}>
                  {value}
                </Row>
              ))}
            </div>
          </div>
        </Card>
      ) : null}
    </div>
  );
}
