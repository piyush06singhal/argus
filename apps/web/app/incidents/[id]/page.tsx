import Link from 'next/link';

import {
  api,
  formatDate,
  type AffectedComponent,
  type Anomaly,
  type ConfigurationContextItem,
  type DeploymentContextItem,
  type Evidence,
  type IncidentSummary,
  type TimelineEvent,
} from '@/lib/api';
import {
  ANOMALY_SEVERITY_STYLES,
  ANOMALY_STATUS_STYLES,
  CLASSIFICATION_LABELS,
  CLASSIFICATION_NOTES,
  CLASSIFICATION_STYLES,
  formatDeviation,
  formatRelativeToFirstAnomaly,
  formatValue,
  NON_CAUSALITY_NOTE,
  SEVERITY_STYLES,
  STATUS_STYLES,
  timelineEventTone,
} from '@/lib/incidents';
import IncidentActions from './IncidentActions';

export const metadata = {
  title: 'Incident Detail',
};

export const dynamic = 'force-dynamic';

function SectionCard({
  title,
  count,
  children,
  subtitle,
}: {
  title: string;
  count?: number;
  children: React.ReactNode;
  subtitle?: string;
}) {
  return (
    <section className="card">
      <div className="mb-3 flex items-baseline justify-between gap-3">
        <h2 className="font-medium text-slate-200">
          {title}
          {count !== undefined ? (
            <span className="ml-2 text-xs font-normal text-slate-500">
              {count}
            </span>
          ) : null}
        </h2>
        {subtitle ? (
          <p className="text-xs text-slate-500">{subtitle}</p>
        ) : null}
      </div>
      {children}
    </section>
  );
}

export default async function IncidentDetailPage({
  params,
}: {
  params: { id: string };
}) {
  const { id } = params;

  try {
    const [
      incident,
      summary,
      timeline,
      anomalies,
      evidence,
      components,
      graph,
      deployments,
      configChanges,
    ] = await Promise.all([
      api.getIncident(id),
      api.getIncidentSummary(id).catch((): IncidentSummary | null => null),
      api.getIncidentTimeline(id),
      api.getIncidentAnomalies(id),
      api.getIncidentEvidence(id),
      api.getIncidentComponents(id),
      api.getIncidentGraph(id),
      api.getIncidentDeployments(id),
      api.getIncidentConfigurationChanges(id),
    ]);

    return (
      <div className="space-y-6">
        <div>
          <Link
            href="/incidents"
            className="text-sm text-slate-400 hover:text-argus-accent"
          >
            ← Back to incidents
          </Link>
          <div className="mt-2 flex flex-wrap items-center gap-3">
            <h1 className="text-2xl font-semibold text-slate-100">
              {incident.title}
            </h1>
            <span
              className={`badge ${
                SEVERITY_STYLES[incident.severity] ?? SEVERITY_STYLES.LOW
              }`}
            >
              {incident.severity}
            </span>
            <span
              className={`badge ${
                STATUS_STYLES[incident.status] ?? STATUS_STYLES.OPEN
              }`}
            >
              {incident.status}
            </span>
          </div>
          <p className="mt-1 text-xs text-slate-500">
            Incident ID: {incident.id}
            {incident.fingerprint ? (
              <>
                {' · fingerprint '}
                <span className="font-mono">
                  {incident.fingerprint.slice(0, 16)}…
                </span>
              </>
            ) : null}
            {incident.status_changed_by
              ? ` · last changed by ${incident.status_changed_by}`
              : ''}
          </p>
          <p className="mt-2 text-sm flex flex-wrap gap-x-4 gap-y-1">
            <Link
              href={`/incidents/${incident.id}/causal-analysis`}
              className="text-argus-accent hover:text-argus-accent-hover"
            >
              Open root cause analysis →
            </Link>
            <Link
              href={`/debugger/incident/${incident.id}?project_id=${encodeURIComponent(incident.project_id)}`}
              className="text-argus-accent hover:text-argus-accent-hover"
            >
              Debug at the code level →
            </Link>
          </p>
        </div>

        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <StatCard label="Detected" value={incident.detected_at} />
          <StatCard label="Started" value={incident.started_at} />
          <StatCard label="Acknowledged" value={incident.acknowledged_at} />
          <StatCard label="Resolved" value={incident.resolved_at} />
        </div>

        <SectionCard
          title="Summary"
          subtitle="Deterministic — generated from stored evidence only"
        >
          <p className="whitespace-pre-line text-sm leading-relaxed text-slate-300">
            {summary?.text ?? incident.summary ?? 'No summary available yet.'}
          </p>
          {summary && summary.generated_from.length > 0 ? (
            <p className="mt-3 text-xs text-slate-500">
              Generated from: {summary.generated_from.join(', ')}
            </p>
          ) : null}
        </SectionCard>

        <IncidentActions incidentId={incident.id} status={incident.status} />

        <SectionCard
          title="Timeline"
          count={timeline.total}
          subtitle="Facts and clearly-marked context, ordered by when they happened"
        >
          {timeline.items.length === 0 ? (
            <p className="text-sm text-slate-400">
              No timeline entries have been recorded for this incident.
            </p>
          ) : (
            <ol className="space-y-3">
              {timeline.items.map((event: TimelineEvent) => (
                <li key={event.id} className="flex gap-3">
                  <span className="mt-1 h-2 w-2 shrink-0 rounded-full bg-argus-accent" />
                  <div className="flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span
                        className={`badge ${timelineEventTone(
                          event.event_type,
                          event.is_context_only
                        )}`}
                      >
                        {event.event_type}
                      </span>
                      <span className="text-xs text-slate-500">
                        {formatDate(event.occurred_at)}
                      </span>
                      {event.is_context_only ? (
                        <span className="text-xs text-slate-500">
                          context only
                        </span>
                      ) : null}
                    </div>
                    <p className="mt-1 text-sm text-slate-200">{event.title}</p>
                    {event.description ? (
                      <p className="mt-0.5 text-xs text-slate-500">
                        {event.description}
                      </p>
                    ) : null}
                  </div>
                </li>
              ))}
            </ol>
          )}
        </SectionCard>

        <SectionCard
          title="Correlated anomalies"
          count={anomalies.total}
          subtitle="Grouping is correlation, not causation"
        >
          {anomalies.items.length === 0 ? (
            <p className="text-sm text-slate-400">
              No anomalies are correlated into this incident.
            </p>
          ) : (
            <div className="overflow-x-auto">
              <table className="min-w-full divide-y divide-slate-800 text-sm">
                <thead>
                  <tr className="text-left text-xs uppercase tracking-wider text-slate-500">
                    <th className="px-3 py-2">Detected</th>
                    <th className="px-3 py-2">Type</th>
                    <th className="px-3 py-2">Severity</th>
                    <th className="px-3 py-2">Observed</th>
                    <th className="px-3 py-2">Expected</th>
                    <th className="px-3 py-2">Deviation</th>
                    <th className="px-3 py-2">Status</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800">
                  {anomalies.items.map((anomaly: Anomaly) => (
                    <tr key={anomaly.id} className="hover:bg-slate-800/40">
                      <td className="whitespace-nowrap px-3 py-2 text-slate-400">
                        {formatDate(anomaly.detected_at)}
                      </td>
                      <td className="px-3 py-2">
                        <Link
                          href={`/anomalies/${anomaly.id}`}
                          className="text-argus-accent hover:text-argus-accent-hover"
                        >
                          {anomaly.anomaly_type}
                        </Link>
                        {anomaly.metric_name ? (
                          <span className="ml-2 font-mono text-xs text-slate-500">
                            {anomaly.metric_name}
                          </span>
                        ) : null}
                      </td>
                      <td className="px-3 py-2">
                        <span
                          className={`badge ${
                            ANOMALY_SEVERITY_STYLES[anomaly.severity] ??
                            ANOMALY_SEVERITY_STYLES.LOW
                          }`}
                        >
                          {anomaly.severity}
                        </span>
                      </td>
                      <td className="px-3 py-2 text-slate-300">
                        {formatValue(anomaly.observed_value)}
                      </td>
                      <td className="px-3 py-2 text-slate-300">
                        {formatValue(anomaly.expected_value)}
                      </td>
                      <td className="px-3 py-2 text-slate-300">
                        {formatDeviation(anomaly.deviation)}
                      </td>
                      <td className="px-3 py-2">
                        <span
                          className={`badge ${
                            ANOMALY_STATUS_STYLES[anomaly.status] ??
                            ANOMALY_STATUS_STYLES.DETECTED
                          }`}
                        >
                          {anomaly.status}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </SectionCard>

        <SectionCard
          title="Affected components"
          count={components.total}
          subtitle="Observed blast radius"
        >
          {components.items.length === 0 ? (
            <p className="text-sm text-slate-400">
              No component impact has been recorded.
            </p>
          ) : (
            <ul className="divide-y divide-slate-800">
              {components.items.map((component: AffectedComponent) => (
                <li
                  key={component.component_id}
                  className="flex flex-wrap items-center justify-between gap-3 py-3"
                >
                  <div>
                    <p className="text-sm font-medium text-slate-200">
                      {component.name ?? component.component_id}
                    </p>
                    <p className="mt-0.5 text-xs text-slate-500">
                      {CLASSIFICATION_NOTES[component.classification] ??
                        component.reason ??
                        'related to this incident'}
                    </p>
                  </div>
                  <span
                    className={`badge ${
                      CLASSIFICATION_STYLES[component.classification] ??
                      CLASSIFICATION_STYLES.DEPENDENCY_CONTEXT
                    }`}
                  >
                    {CLASSIFICATION_LABELS[component.classification] ??
                      component.classification}
                  </span>
                </li>
              ))}
            </ul>
          )}
          <p className="mt-3 text-xs text-slate-500">{NON_CAUSALITY_NOTE}</p>
        </SectionCard>

        <SectionCard
          title="Evidence"
          count={evidence.total}
          subtitle="Each item records why it is relevant"
        >
          {evidence.items.length === 0 ? (
            <p className="text-sm text-slate-400">
              No evidence has been collected for this incident.
            </p>
          ) : (
            <ul className="divide-y divide-slate-800">
              {evidence.items.map((item: Evidence) => (
                <li key={item.id} className="py-3">
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="badge bg-slate-800 text-slate-300">
                        {item.evidence_type}
                      </span>
                      {item.provenance ? (
                        <span className="text-xs text-slate-500">
                          via {item.provenance}
                        </span>
                      ) : null}
                    </div>
                    <span className="text-xs text-slate-500">
                      {formatDate(item.timestamp)}
                    </span>
                  </div>
                  {item.relevance_reason ? (
                    <p className="mt-1 text-sm text-slate-300">
                      {item.relevance_reason}
                    </p>
                  ) : null}
                  {item.observed_value || item.expected_value ? (
                    <p className="mt-1 font-mono text-xs text-slate-400">
                      observed {item.observed_value ?? '—'} · expected{' '}
                      {item.expected_value ?? '—'}
                    </p>
                  ) : null}
                  {item.description ? (
                    <p className="mt-1 text-xs text-slate-500">
                      {item.description}
                    </p>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
        </SectionCard>

        <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
          <SectionCard
            title="Deployment context"
            count={deployments.length}
            subtitle="Temporal context only"
          >
            {deployments.length === 0 ? (
              <p className="text-sm text-slate-400">
                No deployments occurred near this incident.
              </p>
            ) : (
              <ul className="divide-y divide-slate-800">
                {deployments.map((item: DeploymentContextItem) => (
                  <li key={item.deployment_event_id} className="py-3">
                    <p className="text-sm text-slate-200">
                      {item.deployment_id}
                      {item.version ? (
                        <span className="ml-2 text-xs text-slate-500">
                          v{item.version}
                        </span>
                      ) : null}
                    </p>
                    <p className="mt-0.5 text-xs text-slate-500">
                      Occurred{' '}
                      {formatRelativeToFirstAnomaly(
                        item.seconds_before_first_anomaly
                      )}
                    </p>
                  </li>
                ))}
              </ul>
            )}
          </SectionCard>

          <SectionCard
            title="Configuration changes"
            count={configChanges.length}
            subtitle="Temporal context only"
          >
            {configChanges.length === 0 ? (
              <p className="text-sm text-slate-400">
                No configuration changes were recorded near this incident.
              </p>
            ) : (
              <ul className="divide-y divide-slate-800">
                {configChanges.map((item: ConfigurationContextItem) => (
                  <li key={item.configuration_event_id} className="py-3">
                    <p className="text-sm text-slate-200">
                      {item.summary ?? item.configuration_event_id}
                    </p>
                    <p className="mt-0.5 text-xs text-slate-500">
                      {formatDate(item.changed_at)}
                      {item.actor ? ` · source ${item.actor}` : ''}
                    </p>
                  </li>
                ))}
              </ul>
            )}
          </SectionCard>
        </div>

        <SectionCard title="Graph context" subtitle="Structural relationships">
          {graph.nodes.length === 0 ? (
            <p className="text-sm text-slate-400">
              No graph context is available for the affected components.
            </p>
          ) : (
            <>
              <div className="flex flex-wrap gap-2">
                {graph.nodes.map((node) => (
                  <Link
                    key={node.node_id}
                    href={`/system-map?node=${node.node_id}`}
                    className={`badge ${
                      CLASSIFICATION_STYLES[node.classification] ??
                      CLASSIFICATION_STYLES.DEPENDENCY_CONTEXT
                    }`}
                    title={`${node.node_type} · ${
                      CLASSIFICATION_LABELS[node.classification] ??
                      node.classification
                    }`}
                  >
                    {node.name}
                  </Link>
                ))}
              </div>
              <p className="mt-3 text-xs text-slate-500">{graph.disclaimer}</p>
              <p className="mt-1 text-xs text-slate-500">
                {graph.edges.length} relationship
                {graph.edges.length === 1 ? '' : 's'} in this slice.
              </p>
            </>
          )}
        </SectionCard>

        <p className="text-xs text-slate-500">
          This page reports the observed facts, their correlation and the
          surrounding context — it does not determine root-cause. The
          evidence-supported explanations (candidates, the causal chain and the
          contradicting evidence) live in{' '}
          <Link
            href={`/incidents/${incident.id}/causal-analysis`}
            className="text-argus-accent hover:text-argus-accent-hover"
          >
            root cause analysis
          </Link>
          . Once a hypothesis exists, it can be tested in an isolated sandbox
          through{' '}
          <Link
            href={`/incidents/${incident.id}/reproductions`}
            className="text-argus-accent hover:text-argus-accent-hover"
          >
            failure reproduction
          </Link>
          .
        </p>
      </div>
    );
  } catch (error) {
    const message =
      error instanceof Error ? error.message : 'Unknown error occurred';
    return (
      <div className="space-y-6">
        <Link
          href="/incidents"
          className="text-sm text-slate-400 hover:text-argus-accent"
        >
          ← Back to incidents
        </Link>
        <div className="card border-argus-error/40">
          <h2 className="font-medium text-argus-error">
            Failed to load incident
          </h2>
          <p className="mt-2 text-sm text-slate-400">
            Could not load incident {id}. ({message})
          </p>
        </div>
      </div>
    );
  }
}

function StatCard({
  label,
  value,
}: {
  label: string;
  value: string | null | undefined;
}) {
  return (
    <div className="card">
      <p className="text-xs font-medium uppercase tracking-wider text-slate-500">
        {label}
      </p>
      <p
        className={`mt-2 text-sm ${value ? 'text-slate-300' : 'text-slate-600'}`}
      >
        {value ? formatDate(value) : '—'}
      </p>
    </div>
  );
}
