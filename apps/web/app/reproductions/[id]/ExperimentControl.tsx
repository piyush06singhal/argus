'use client';

/**
 * The experiment's execution gate and live view (§47, §48).
 *
 * Two deliberate choices:
 *
 * 1. **Nothing starts implicitly.** The start button stays disabled until the
 *    engineer ticks the confirmation box *and* the server says the experiment is
 *    startable. A plan existing is not consent to run it.
 * 2. **Polling, not new infrastructure.** The backend exposes a plain status
 *    endpoint, so the page polls it only while the experiment is genuinely in
 *    flight and stops the moment it is terminal. That avoids adding a websocket
 *    channel for a page that is open for minutes, not hours.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';

import {
  api,
  type ExperimentStatus,
  type ReproductionExperimentDetail,
  type ReproductionSafetyPreview,
  type ReproductionStatus,
} from '@/lib/api';
import {
  EXPERIMENT_HAPPY_PATH,
  experimentStatusStyle,
  failureClassLabel,
  formatSeconds,
  isTerminalStatus,
  formatBytes,
} from '@/lib/reproduction';

const POLL_INTERVAL_MS = 3000;

export default function ExperimentControl({
  detail,
  safety,
  projectId,
}: {
  detail: ReproductionExperimentDetail;
  safety: ReproductionSafetyPreview;
  projectId?: string;
}) {
  const router = useRouter();
  const [experiment, setExperiment] = useState(detail.experiment);
  const [live, setLive] = useState<ReproductionStatus | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState<'start' | 'cancel' | 'retry' | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const scope = projectId ?? experiment.project_id;
  const status: ExperimentStatus = experiment.status;
  const terminal = isTerminalStatus(status);
  const running = !terminal && status !== 'PLANNED';

  // A ref keeps the poller reading the latest status without re-arming itself
  // on every render, which would otherwise reset the interval continuously.
  const inFlight = useRef(running);
  inFlight.current = running;

  const poll = useCallback(async () => {
    try {
      const snapshot = await api.getReproductionStatus(experiment.id, scope);
      setLive(snapshot);
      if (snapshot.status !== experiment.status) {
        setExperiment((previous) => ({ ...previous, status: snapshot.status }));
      }
      if (isTerminalStatus(snapshot.status)) {
        // Re-render from the server so every section (comparison, validation,
        // artifacts) reflects the finished experiment, not a stale shell.
        router.refresh();
      }
    } catch {
      // A transient polling failure is not worth surfacing: the next tick
      // retries, and the panel keeps showing the last known state.
    }
  }, [experiment.id, experiment.status, scope, router]);

  useEffect(() => {
    if (!inFlight.current) {
      return;
    }
    void poll();
    const timer = setInterval(() => {
      if (!inFlight.current) {
        clearInterval(timer);
        return;
      }
      void poll();
    }, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [poll, running]);

  const start = async () => {
    setBusy('start');
    setError(null);
    setNotice(null);
    try {
      await api.startReproduction(experiment.id, scope, 'ui');
      setNotice('Queued. The sandbox is provisioned by the worker.');
      router.refresh();
    } catch (cause: unknown) {
      setError(
        cause instanceof Error ? cause.message : 'The experiment could not start.'
      );
    } finally {
      setBusy(null);
    }
  };

  const cancel = async () => {
    setBusy('cancel');
    setError(null);
    try {
      await api.cancelReproduction(
        experiment.id,
        scope,
        'cancelled from the reproduction workspace'
      );
      setNotice(
        'Cancellation requested. The running repetition unwinds through its ' +
          'cleanup path so the sandbox is destroyed.'
      );
      router.refresh();
    } catch (cause: unknown) {
      setError(
        cause instanceof Error
          ? cause.message
          : 'The experiment could not be cancelled.'
      );
    } finally {
      setBusy(null);
    }
  };

  const retry = async () => {
    setBusy('retry');
    setError(null);
    try {
      const created = await api.retryReproduction(experiment.id, scope, {
        requested_by: 'ui',
      });
      router.push(`/reproductions/${created.experiment.id}`);
    } catch (cause: unknown) {
      setError(
        cause instanceof Error
          ? cause.message
          : 'A fresh experiment could not be planned.'
      );
      setBusy(null);
    }
  };

  const step = live?.progress.step ?? null;

  return (
    <section className="card">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-3">
        <h2 className="font-medium text-slate-200">
          Experiment v{experiment.experiment_version}
        </h2>
        <div className="flex flex-wrap items-center gap-2">
          <span className={`badge ${experimentStatusStyle(status)}`}>{status}</span>
          <span className="text-xs text-slate-500">
            {experiment.repetitions} repetition(s) · engine{' '}
            {experiment.engine_version ?? '—'}
          </span>
        </div>
      </div>

      {status === 'PLANNED' ? (
        <div className="space-y-4">
          <div className="rounded-md border border-slate-800 bg-slate-950/40 p-4">
            <p className="text-xs uppercase tracking-wider text-slate-500">
              Safety confirmation
            </p>
            <dl className="mt-2 grid grid-cols-1 gap-2 text-sm sm:grid-cols-2">
              <Row label="Sandbox" value={safety.sandbox} mono />
              <Row label="Backend" value={safety.backend} />
              <Row label="Network" value={safety.network_policy} />
              <Row label="Production access" value={safety.production_access} />
              <Row label="Credentials" value={safety.credentials} />
              <Row
                label="Timeout"
                value={`${safety.timeout_seconds}s`}
              />
              <Row label="Repetitions" value={String(safety.repetitions)} />
              <Row
                label="Resource limits"
                value={Object.entries(safety.resource_limits)
                  .map(([key, value]) => `${key}=${value}`)
                  .join(' · ')}
              />
            </dl>
            <p className="mt-3 text-xs uppercase tracking-wider text-slate-500">
              Services started in the sandbox
            </p>
            <p className="mt-1 text-sm text-slate-300">
              {safety.services.length > 0 ? safety.services.join(', ') : '—'}
            </p>
            {safety.faults.length > 0 ? (
              <>
                <p className="mt-3 text-xs uppercase tracking-wider text-slate-500">
                  Faults to inject
                </p>
                <ul className="mt-1 space-y-1 text-sm text-slate-300">
                  {safety.faults.map((fault, index) => (
                    <li key={`${fault.fault_type}-${fault.target}-${index}`}>
                      {fault.fault_type} → {fault.target} ({fault.status}
                      {fault.injected ? ' · injected' : ' · not injected'})
                    </li>
                  ))}
                </ul>
              </>
            ) : null}
          </div>

          {safety.warnings.length > 0 ? (
            <ul className="space-y-1 text-xs text-argus-warning">
              {safety.warnings.map((warning) => (
                <li key={warning}>⚠ {warning}</li>
              ))}
            </ul>
          ) : null}

          {safety.blocked_reasons.length > 0 ? (
            <ul className="space-y-1 text-xs text-argus-error">
              {safety.blocked_reasons.map((reason) => (
                <li key={reason}>✕ {reason}</li>
              ))}
            </ul>
          ) : null}

          <label className="flex items-start gap-2 text-sm text-slate-300">
            <input
              type="checkbox"
              className="mt-1"
              checked={confirmed}
              onChange={(event) => setConfirmed(event.target.checked)}
            />
            <span>
              I understand this runs an isolated experiment. ARGUS will start the
              named services in a disposable sandbox with production access
              blocked and credentials sanitized.
            </span>
          </label>

          <div className="flex flex-wrap items-center gap-3">
            <button
              type="button"
              onClick={start}
              disabled={!confirmed || !safety.can_start || busy !== null}
              className="btn-primary"
            >
              {busy === 'start' ? 'Starting…' : 'Start experiment'}
            </button>
            <span className="text-xs text-slate-500">
              The plan itself executed nothing. Cancellation is always available
              once it is running.
            </span>
          </div>
        </div>
      ) : (
        <div className="space-y-4">
          <div>
            <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-slate-500">
              <span>
                {live?.progress.percent ?? 0}% · step {step ?? '—'} of{' '}
                {EXPERIMENT_HAPPY_PATH.length}
              </span>
              <span>
                elapsed{' '}
                {formatSeconds(
                  live?.progress.elapsed_seconds ??
                    (experiment.started_at
                      ? (Date.now() - Date.parse(experiment.started_at)) / 1000
                      : null)
                )}
                {experiment.timeout_at
                  ? ` · deadline ${new Date(experiment.timeout_at).toLocaleTimeString()}`
                  : ''}
              </span>
            </div>
            <div className="mt-2 h-2 w-full overflow-hidden rounded-full bg-slate-800">
              <div
                className="h-full bg-argus-accent transition-all"
                style={{ width: `${live?.progress.percent ?? 0}%` }}
              />
            </div>
          </div>

          <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
            <Row
              label="Sandbox"
              value={
                live?.sandbox?.status
                  ? `${live.sandbox.sandbox_key} (${live.sandbox.status})`
                  : safety.sandbox
              }
              mono
            />
            <Row
              label="Replay"
              value={`${live?.replay_completed ?? 0} / ${
                live?.replay_total ?? 0
              } item(s)`}
            />
            <Row
              label="Faults"
              value={`${live?.faults_active ?? 0} active / ${
                live?.faults_total ?? 0
              }`}
            />
            <Row
              label="Runs"
              value={`${live?.runs_completed ?? experiment.completed_runs} / ${
                experiment.repetitions
              }`}
            />
            <Row
              label="Requests"
              value={String(live?.latest_run?.replay_request_count ?? 0)}
            />
            <Row
              label="Failures"
              value={String(live?.latest_run?.replay_failure_count ?? 0)}
            />
            <Row
              label="Rejected by safety"
              value={String(live?.latest_run?.replay_rejected_count ?? 0)}
            />
            <Row
              label="Telemetry"
              value={formatBytes(
                live?.latest_run?.telemetry_bytes ??
                  live?.resources.observed?.telemetry_bytes ??
                  null
              )}
            />
          </dl>

          {live?.latest_run?.error ? (
            <p className="text-xs text-argus-error">{live.latest_run.error}</p>
          ) : null}
          {live?.latest_run?.failure_classification ? (
            <p className="text-xs text-argus-warning">
              Classified as{' '}
              {failureClassLabel(live.latest_run.failure_classification)} — a null
              result this class explains is not a refutation of the hypothesis.
            </p>
          ) : null}

          <div className="flex flex-wrap items-center gap-3">
            {running ? (
              <button
                type="button"
                onClick={cancel}
                disabled={busy !== null}
                className="btn-ghost"
              >
                {busy === 'cancel' ? 'Cancelling…' : 'Cancel experiment'}
              </button>
            ) : (
              <button
                type="button"
                onClick={retry}
                disabled={busy !== null}
                className="btn-primary"
              >
                {busy === 'retry' ? 'Planning…' : 'Plan another attempt'}
              </button>
            )}
            <span className="text-xs text-slate-500">
              {running
                ? 'Cancellation is cooperative: the repetition unwinds through cleanup so no sandbox is left behind.'
                : 'A retry plans a new version — this experiment stays as the record of what happened.'}
            </span>
          </div>
        </div>
      )}

      {notice ? <p className="mt-3 text-xs text-slate-400">{notice}</p> : null}
      {error ? <p className="mt-3 text-xs text-argus-error">{error}</p> : null}
    </section>
  );
}

function Row({
  label,
  value,
  mono,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wider text-slate-500">{label}</dt>
      <dd className={`text-slate-200 ${mono ? 'font-mono text-xs' : ''}`}>
        {value || '—'}
      </dd>
    </div>
  );
}
