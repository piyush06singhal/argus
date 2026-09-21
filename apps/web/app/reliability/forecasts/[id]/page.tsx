import Link from 'next/link';

import {
  api,
  formatDate,
  type Forecast,
} from '@/lib/api';
import {
  confidenceLabel,
  coverageLabel,
  dataQualityLabel,
  dataQualityStyle,
  forecastStatusStyle,
  FORECAST_STATUS_LABELS,
  HORIZON_LABELS,
  OUTCOME_LABELS,
  outcomeStyle,
  riskLevelStyle,
  riskPhrase,
  riskScoreLabel,
  signalSeverityStyle,
  signalTypeLabel,
  topSignals,
  trendArrow,
  FORECAST_DISCLAIMER,
} from '@/lib/reliability';

export const metadata = {
  title: 'Forecast detail',
};

export const dynamic = 'force-dynamic';

function Card({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="card">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-3">
        <h2 className="font-medium text-slate-200">{title}</h2>
        {subtitle ? <p className="text-xs text-slate-500">{subtitle}</p> : null}
      </div>
      {children}
    </section>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wider text-slate-500">{label}</dt>
      <dd className="text-slate-200">{value}</dd>
    </div>
  );
}

/**
 * The forecast detail page (§48, §49, §50).
 *
 * The layout follows the four questions of §34 in order: the claim (with its
 * horizon, model and data quality), the explanation (what changed and why),
 * the evidence (signals, feature snapshot, similar incidents) and the outcome
 * (what actually happened). Every section is honest about absence.
 */
export default async function ForecastDetailPage({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams: { project_id?: string };
}) {
  const { id } = await params;
  const projectId =
    typeof searchParams.project_id === 'string' ? searchParams.project_id : '';

  let forecast: Forecast | null = null;
  let explanation: Record<string, unknown> | null = null;
  let outcome: Record<string, unknown> | null = null;
  let snapshot: Record<string, unknown> | null = null;
  let error: string | null = null;

  try {
    forecast = await api.getForecast(id, projectId || undefined);
    const scope = projectId || forecast.project_id;
    const [explanationData, outcomeData] = await Promise.all([
      api.getForecastExplanation(id, scope).catch(() => null),
      api.getForecastOutcome(id, scope).catch(() => null),
    ]);
    explanation = explanationData as unknown as Record<string, unknown> | null;
    outcome = outcomeData as unknown as Record<string, unknown> | null;
    snapshot = forecast.feature_snapshot_id
      ? ((await api
          .getForecastSnapshot(id, scope)
          .catch(() => null)) as unknown as Record<string, unknown> | null)
      : null;
  } catch (cause) {
    error = cause instanceof Error ? cause.message : String(cause);
  }

  if (error !== null || !forecast) {
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-semibold text-slate-100">Forecast</h1>
        <Card title="Not available" subtitle="The forecast could not be loaded">
          <p className="text-sm text-argus-error">
            {error ?? 'Forecast not found in this scope.'}
          </p>
          <p className="mt-2 text-xs text-slate-500">
            An out-of-scope id answers not-found rather than confirming existence.
          </p>
        </Card>
      </div>
    );
  }

  const signals = topSignals(forecast.signals ?? [], 8);
  const whatChanged = Array.isArray(explanation?.what_changed)
    ? (explanation?.what_changed as string[])
    : [];
  const uncertain = Array.isArray(explanation?.what_is_uncertain)
    ? (explanation?.what_is_uncertain as string[])
    : [...(forecast.limitations ?? [])];
  const caveats = Array.isArray(explanation?.caveats)
    ? (explanation?.caveats as string[])
    : [];
  const historical = Array.isArray(explanation?.historical_evidence)
    ? (explanation?.historical_evidence as Array<Record<string, unknown>>)
    : [];
  const whyChanged = (explanation?.why_risk_changed ?? {}) as {
    direction?: string;
    details?: string[];
    previous_risk_level?: string | null;
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-100">{forecast.headline}</h1>
        <p className="mt-1 text-sm text-slate-400">
          {riskPhrase(forecast.risk_level, HORIZON_LABELS[forecast.forecast_horizon])} ·{' '}
          {forecast.prediction_type}
        </p>
        <p className="mt-2 text-xs text-slate-500">{FORECAST_DISCLAIMER}</p>
      </div>

      <Card title="The claim" subtitle="Everything needed to judge it">
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <span className={`badge ${riskLevelStyle(forecast.risk_level)}`}>
            {forecast.risk_level}
          </span>
          <span className="badge bg-slate-800 text-slate-300">
            score {riskScoreLabel(forecast.risk_score)}
          </span>
          <span className={`badge ${dataQualityStyle(forecast.data_quality)}`}>
            data: {dataQualityLabel(forecast.data_quality)}
          </span>
          <span className={`badge ${forecastStatusStyle(forecast.status)}`}>
            {FORECAST_STATUS_LABELS[forecast.status]}
          </span>
        </div>
        <dl className="grid grid-cols-2 gap-4 md:grid-cols-4">
          <Metric
            label="Horizon"
            value={HORIZON_LABELS[forecast.forecast_horizon] ?? forecast.forecast_horizon}
          />
          <Metric label="Model" value={forecast.model_version_label} />
          <Metric
            label="Generated"
            value={formatDate(forecast.generated_at)}
          />
          <Metric label="Valid until" value={formatDate(forecast.valid_until)} />
          <Metric
            label="Confidence"
            value={confidenceLabel(forecast.confidence, forecast.confidence_reason)}
          />
          <Metric label="Calibration" value={forecast.calibration_status} />
          <Metric label="Coverage" value={coverageLabel(forecast.data_coverage)} />
          <Metric label="Revision" value={`#${forecast.revision}`} />
        </dl>
        {forecast.summary ? (
          <p className="mt-3 text-sm text-slate-400">{forecast.summary}</p>
        ) : null}
        {forecast.failure_reason ? (
          <p className="mt-3 rounded border border-slate-800 bg-slate-900/50 p-3 text-sm text-slate-400">
            <span className="text-slate-300">No prediction was made:</span>{' '}
            {forecast.failure_detail ?? forecast.failure_reason}
          </p>
        ) : null}
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="What changed" subtitle="The measured movement behind the claim">
          {whatChanged.length === 0 ? (
            <p className="text-sm text-slate-400">
              No measured movement was recorded for this forecast.
            </p>
          ) : (
            <ul className="list-inside list-disc space-y-1 text-sm text-slate-300">
              {whatChanged.map((item, index) => (
                <li key={index}>{item}</li>
              ))}
            </ul>
          )}
          {whyChanged.direction && whyChanged.direction !== 'NEW' ? (
            <p className="mt-3 text-xs text-slate-500">
              Risk {whyChanged.direction.toLowerCase()} from{' '}
              {whyChanged.previous_risk_level ?? 'unknown'} to{' '}
              {forecast.risk_level}
              {whyChanged.details?.length
                ? ` — ${whyChanged.details.join('; ')}`
                : ''}
            </p>
          ) : null}
        </Card>

        <Card title="What is uncertain" subtitle="Stated with the claim, not in a footnote">
          {uncertain.length === 0 ? (
            <p className="text-sm text-slate-400">
              No limitations were recorded for this forecast.
            </p>
          ) : (
            <ul className="list-inside list-disc space-y-1 text-sm text-slate-300">
              {uncertain.map((item, index) => (
                <li key={index}>{item}</li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      <Card
        title="Predictive signals"
        subtitle="Signals are evidence of rising risk — they are not causes (§6)"
      >
        {signals.length === 0 ? (
          <p className="text-sm text-slate-400">
            This forecast carries no signals (its evidence did not cross a
            threshold).
          </p>
        ) : (
          <ul className="space-y-3">
            {signals.map((signal) => (
              <li
                key={signal.id}
                className="rounded border border-slate-800 bg-slate-900/50 p-3"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className={`badge ${signalSeverityStyle(signal.severity)}`}>
                    #{signal.rank + 1} {signalTypeLabel(signal.signal_type)}
                  </span>
                  <span className="text-xs text-slate-500">
                    {trendArrow(signal.trend)}
                  </span>
                  {signal.similar_incident_count > 0 ? (
                    <span className="text-xs text-slate-500">
                      {signal.similar_incident_count} similar past incident(s)
                    </span>
                  ) : null}
                </div>
                <p className="mt-1 text-sm text-slate-300">{signal.description}</p>
                <p className="mt-1 text-xs text-slate-500">
                  {signal.observed_value != null
                    ? `observed ${signal.observed_value}`
                    : 'observed value unavailable'}
                  {signal.baseline_value != null
                    ? ` · baseline ${signal.baseline_value}`
                    : ''}
                  {signal.metric_name ? ` · ${signal.metric_name}` : ''}
                </p>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card
          title="Similar historical incidents"
          subtitle="Similarity is not a prediction of repeat"
        >
          {historical.length === 0 ? (
            <p className="text-sm text-slate-400">
              No structurally similar past incidents were found before this
              forecast was generated.
            </p>
          ) : (
            <ul className="space-y-2 text-sm text-slate-300">
              {historical.map((item, index) => (
                <li
                  key={index}
                  className="rounded border border-slate-800 bg-slate-900/50 p-3"
                >
                  <span className="text-slate-200">
                    {String(item.title ?? 'incident')}
                  </span>
                  <span className="ml-2 text-xs text-slate-500">
                    {String(item.detected_at ?? '')}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card title="What actually happened" subtitle="Scored once the horizon elapsed">
          {outcome === null ? (
            <p className="text-sm text-slate-400">
              Not scored yet — the outcome exists only after the horizon has
              elapsed and an evaluation pass has run.
            </p>
          ) : (
            <div>
              <span
                className={`badge ${
                  outcomeStyle(
                    String(outcome.outcome) as keyof typeof OUTCOME_LABELS & string
                  )
                }`}
              >
                {OUTCOME_LABELS[
                  String(outcome.outcome) as keyof typeof OUTCOME_LABELS
                ] ?? String(outcome.outcome)}
              </span>
              <p className="mt-2 text-sm text-slate-400">
                {String(outcome.evaluation_reason ?? '')}
              </p>
              {outcome.time_to_event_seconds != null ? (
                <p className="mt-1 text-xs text-slate-500">
                  lead time: {Math.round(Number(outcome.time_to_event_seconds) / 60)} min
                </p>
              ) : null}
            </div>
          )}
        </Card>
      </div>

      <Card
        title="Feature snapshot"
        subtitle="The exact inputs — the forecast is reproducible from them (§17)"
      >
        {snapshot === null ? (
          <p className="text-sm text-slate-400">
            No feature snapshot is attached to this forecast.
          </p>
        ) : (
          <div>
            <dl className="grid grid-cols-2 gap-4 md:grid-cols-4">
              <Metric
                label="Schema version"
                value={String(snapshot.feature_schema_version ?? '—')}
              />
              <Metric label="Samples" value={String(snapshot.sample_count ?? 0)} />
              <Metric
                label="Window start"
                value={
                  snapshot.feature_window_start
                    ? formatDate(String(snapshot.feature_window_start))
                    : '—'
                }
              />
              <Metric
                label="Window end"
                value={
                  snapshot.feature_window_end
                    ? formatDate(String(snapshot.feature_window_end))
                    : '—'
                }
              />
            </dl>
            {Array.isArray(snapshot.data_quality_notes) &&
            snapshot.data_quality_notes.length > 0 ? (
              <ul className="mt-3 list-inside list-disc text-xs text-slate-500">
                {snapshot.data_quality_notes.map((note, index) => (
                  <li key={index}>{note}</li>
                ))}
              </ul>
            ) : null}
          </div>
        )}
      </Card>

      <div>
        <Link
          href={`/reliability?project_id=${encodeURIComponent(projectId || forecast.project_id)}`}
          className="text-xs text-argus-accent hover:underline"
        >
          ← Reliability dashboard
        </Link>
      </div>
    </div>
  );
}
