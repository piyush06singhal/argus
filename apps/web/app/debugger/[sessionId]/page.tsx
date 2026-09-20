import Link from 'next/link';

import {
  api,
  formatDate,
  type DebugAnalysis,
  type DebugHypothesis,
  type DebugSessionDetail,
} from '@/lib/api';
import {
  analysisStatusStyle,
  confidenceStyle,
  evidencePolarityStyle,
  formatBytes,
  hypothesisValidationStyle,
  locationValidationStyle,
  LOCATION_LABEL_NOTE,
  locationLabelNote,
  partitionLocations,
  rankHypotheses,
  sessionStatusStyle,
} from '@/lib/debugger';
import AskPanel from '../AskPanel';

export const metadata = {
  title: 'Debug Session',
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

/**
 * One debugging session's workspace (§34, §38).
 *
 * Layout mirrors the phase's epistemics: session facts first, the analysis
 * run's audit trail (bounds, tool calls, degraded reason) before its
 * findings, verified locations split from rejected claims, hypotheses ranked
 * by validation status rather than model confidence, and the messages log
 * last.
 */
export default async function DebugSessionPage({
  params,
  searchParams,
}: {
  params: { sessionId: string };
  searchParams: { project_id?: string };
}) {
  const { sessionId } = params;
  const projectId =
    typeof searchParams.project_id === 'string'
      ? searchParams.project_id
      : undefined;

  let session: DebugSessionDetail | null = null;
  let error: string | null = null;

  try {
    session = await api.getDebugSession(sessionId, projectId);
  } catch (cause: unknown) {
    error = cause instanceof Error ? cause.message : 'unknown error';
  }

  if (session === null) {
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-semibold text-slate-100">
          Debug session
        </h1>
        <Card title="Session unavailable">
          <p className="text-sm text-slate-400">{error ?? 'Unknown error.'}</p>
          <p className="mt-2 text-xs text-slate-500">
            <Link href="/debugger" className="text-argus-accent underline">
              ← Back to the debugger
            </Link>
          </p>
        </Card>
      </div>
    );
  }

  const scope = session.project_id;
  const analysis = session.latest_analysis ?? null;

  return (
    <div className="space-y-6">
      <div>
        <p className="text-xs uppercase tracking-wide text-slate-500">
          Session for incident{' '}
          <Link
            href={`/debugger/incident/${session.incident_id}?project_id=${encodeURIComponent(scope)}`}
            className="text-argus-accent underline"
          >
            {session.incident_id.slice(0, 8)}
          </Link>
        </p>
        <h1 className="mt-1 text-2xl font-semibold text-slate-100">
          {session.title ?? `Session ${session.id.slice(0, 8)}`}
        </h1>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <span className={`badge ${sessionStatusStyle(session.status)}`}>
            {session.status}
          </span>
          <span className="badge bg-slate-800 text-slate-300">
            code version: {session.version_status}
          </span>
          {session.snapshot_id ? (
            <span className="badge bg-slate-800 text-slate-300">
              snapshot {session.snapshot_id.slice(0, 8)}
            </span>
          ) : null}
          <span className="badge bg-slate-800 text-slate-300">
            context {session.context_version.slice(0, 12)}
          </span>
        </div>
        {session.version_note ? (
          <p className="mt-2 text-xs text-slate-500">{session.version_note}</p>
        ) : null}
      </div>

      <AnalysisSection analysis={analysis} />

      <HypothesesSection analysis={analysis} />

      <MessagesSection session={session} />

      <TimelineSection sessionId={session.id} projectId={scope} />
    </div>
  );
}

function AnalysisSection({ analysis }: { analysis: DebugAnalysis | null }) {
  if (analysis === null) {
    return (
      <Card title="Analysis" subtitle="No analysis run yet">
        <p className="text-sm text-slate-400">
          This session has no analysis yet. Run one from the incident page or
          ask a question below — the deterministic investigation is used
          whenever the model provider is unavailable.
        </p>
      </Card>
    );
  }

  return (
    <Card
      title="Analysis"
      subtitle={`run ${analysis.id.slice(0, 8)} · ${analysis.kind} · ${analysis.provider_name ?? 'deterministic'}${analysis.model_name ? ` / ${analysis.model_name}` : ''}`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className={`badge ${analysisStatusStyle(analysis.status)}`}>
          {analysis.status}
        </span>
        <span className={`badge ${confidenceStyle(analysis.confidence)}`}>
          confidence: {analysis.confidence}
        </span>
        <span className="badge bg-slate-800 text-slate-300">
          {analysis.tool_call_count} tool calls
        </span>
        <span className="badge bg-slate-800 text-slate-300">
          {analysis.files_accessed} files accessed
        </span>
        {analysis.context_bytes ? (
          <span className="badge bg-slate-800 text-slate-300">
            context {formatBytes(analysis.context_bytes)}
          </span>
        ) : null}
        <span className="badge bg-slate-800 text-slate-300">
          prompt {analysis.prompt_version}
        </span>
        {analysis.duration_ms != null ? (
          <span className="badge bg-slate-800 text-slate-300">
            {(analysis.duration_ms / 1000).toFixed(1)}s
          </span>
        ) : null}
      </div>

      {analysis.degraded ? (
        <p className="mt-3 rounded-md border border-argus-warning/40 bg-argus-warning/5 p-3 text-sm text-argus-warning">
          Degraded run — {analysis.degraded_reason ?? 'reason not recorded'}.
          The deterministic investigation was used instead of the model.
        </p>
      ) : null}

      {analysis.summary ? (
        <p className="mt-3 whitespace-pre-wrap text-sm text-slate-300">
          {analysis.summary}
        </p>
      ) : null}

      {analysis.recommended_inspections.length > 0 ? (
        <div className="mt-3">
          <p className="text-xs uppercase tracking-wide text-slate-500">
            Recommended inspections
          </p>
          <ul className="mt-1 list-disc space-y-1 pl-5 text-sm text-slate-300">
            {analysis.recommended_inspections.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {analysis.missing_evidence.length > 0 ? (
        <p className="mt-3 text-xs text-slate-500">
          Missing evidence: {analysis.missing_evidence.join(' · ')}
        </p>
      ) : null}

      {analysis.invalid_references.length > 0 ? (
        <p className="mt-2 text-xs text-argus-error">
          {analysis.invalid_references.length} cited reference(s) failed
          validation and were excluded from the findings.
        </p>
      ) : null}
    </Card>
  );
}

function HypothesesSection({ analysis }: { analysis: DebugAnalysis | null }) {
  if (analysis === null) {
    return null;
  }

  const ranked = rankHypotheses(analysis.hypotheses);
  const { findings, rejected } = partitionLocations(analysis.locations);

  return (
    <Card
      title="Hypotheses & locations"
      subtitle="ranked by validation status — never by model confidence alone (§28)"
    >
      {ranked.length === 0 ? (
        <p className="text-sm text-slate-400">
          No hypotheses were produced. The analysis records why in its
          limitations rather than inventing one.
        </p>
      ) : (
        <ol className="space-y-4">
          {ranked.map((hypothesis, index) => (
            <HypothesisCard
              key={hypothesis.id}
              hypothesis={hypothesis}
              index={index + 1}
            />
          ))}
        </ol>
      )}

      {rejected.length > 0 ? (
        <div className="mt-4 border-t border-slate-800 pt-3">
          <p className="text-xs uppercase tracking-wide text-slate-500">
            Rejected code claims ({rejected.length}) — shown for audit, not as
            findings
          </p>
          <ul className="mt-2 space-y-1">
            {rejected.map((location) => (
              <li
                key={location.id}
                className="flex flex-wrap items-center gap-2 text-xs text-slate-500"
              >
                <span
                  className={`badge ${locationValidationStyle(location.validation)}`}
                >
                  {location.validation}
                </span>
                <code className="text-slate-400">
                  {location.file_path}
                  {location.start_line != null
                    ? `:${location.start_line}${location.end_line != null && location.end_line !== location.start_line ? `-${location.end_line}` : ''}`
                    : ''}
                </code>
                <span>{location.reason}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {findings.length > 0 ? (
        <p className="mt-3 text-xs text-slate-500">{LOCATION_LABEL_NOTE}</p>
      ) : null}
    </Card>
  );
}

function HypothesisCard({
  hypothesis,
  index,
}: {
  hypothesis: DebugHypothesis;
  index: number;
}) {
  return (
    <li className="rounded-md border border-slate-800 bg-slate-900/60 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium text-slate-200">
          {index}. {hypothesis.description}
        </span>
        <span
          className={`badge ${hypothesisValidationStyle(hypothesis.validation_status)}`}
          title="Decided by resolved stored evidence, not by the model"
        >
          {hypothesis.validation_status}
        </span>
        <span className={`badge ${confidenceStyle(hypothesis.confidence)}`}>
          {hypothesis.confidence}
        </span>
        <span className="badge bg-slate-800 text-slate-400">
          {hypothesis.category}
        </span>
        {hypothesis.recurrence_count > 1 ? (
          <span className="badge bg-slate-800 text-slate-400">
            seen {hypothesis.recurrence_count}×
          </span>
        ) : null}
      </div>

      {hypothesis.rationale ? (
        <p className="mt-2 text-sm text-slate-400">{hypothesis.rationale}</p>
      ) : null}

      {hypothesis.testable && hypothesis.test_approach ? (
        <p className="mt-2 text-xs text-slate-400">
          Test: {hypothesis.test_approach}
        </p>
      ) : null}

      {hypothesis.locations.length > 0 ? (
        <div className="mt-3">
          <p className="text-xs uppercase tracking-wide text-slate-500">
            Locations ({hypothesis.locations.length}) — each validated against
            the pinned snapshot
          </p>
          <ul className="mt-1 space-y-1">
            {hypothesis.locations.map((location) => (
              <li key={location.id} className="text-xs">
                <span
                  className={`badge ${locationValidationStyle(location.validation)}`}
                  title={`${location.validation}: ${location.validation_detail ?? locationLabelNote(location.label)}`}
                >
                  {location.validation}
                </span>{' '}
                <code className="text-slate-300">
                  {location.file_path}
                  {location.start_line != null
                    ? `:${location.start_line}${location.end_line != null && location.end_line !== location.start_line ? `-${location.end_line}` : ''}`
                    : ''}
                </code>
                {location.symbol_name ? (
                  <span className="text-slate-400"> · {location.symbol_name}</span>
                ) : null}
                <span className="ml-2 text-slate-500">{location.label}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      <EvidenceList
        title="Supporting"
        items={hypothesis.supporting_evidence}
      />
      <EvidenceList
        title="Contradicting"
        items={hypothesis.contradicting_evidence}
      />
    </li>
  );
}

function EvidenceList({
  title,
  items,
}: {
  title: string;
  items: DebugHypothesis['supporting_evidence'];
}) {
  if (items.length === 0) {
    return null;
  }
  return (
    <div className="mt-3">
      <p className="text-xs uppercase tracking-wide text-slate-500">
        {title} evidence ({items.length})
      </p>
      <ul className="mt-1 space-y-1">
        {items.map((evidence) => (
          <li key={evidence.id} className="flex flex-wrap items-center gap-2 text-xs">
            <span className={`badge ${evidencePolarityStyle(evidence.polarity)}`}>
              {evidence.kind}
            </span>
            {evidence.valid ? null : (
              <span className="badge bg-argus-error/15 text-argus-error">
                unresolved
              </span>
            )}
            <code className="text-slate-400">{evidence.reference}</code>
            {evidence.quote ? (
              <span className="text-slate-500">“{evidence.quote}”</span>
            ) : null}
          </li>
        ))}
      </ul>
    </div>
  );
}

function MessagesSection({ session }: { session: DebugSessionDetail }) {
  return (
    <Card
      title="Conversation"
      subtitle="§35–§36 — answers cite only stored, resolvable evidence"
    >
      {session.messages.length > 0 ? (
        <ul className="space-y-3">
          {session.messages.map((message) => (
            <li
              key={message.id}
              className={`rounded-md border p-3 text-sm ${
                message.role === 'ENGINEER'
                  ? 'border-slate-700 bg-slate-800/40 text-slate-200'
                  : message.role === 'SYSTEM'
                    ? 'border-slate-800 bg-slate-900/60 text-slate-400'
                    : 'border-argus-accent/30 bg-argus-accent/5 text-slate-200'
              }`}
            >
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <span className="text-xs uppercase tracking-wide text-slate-500">
                  {message.role}
                </span>
                <span className="text-xs text-slate-600">
                  {formatDate(message.created_at)}
                </span>
              </div>
              <p className="mt-1 whitespace-pre-wrap">{message.content}</p>
              {message.evidence_refs.length > 0 ? (
                <p className="mt-2 flex flex-wrap gap-1 text-xs text-slate-500">
                  Cited:
                  {message.evidence_refs.map((ref) => (
                    <code key={ref} className="text-slate-500">
                      {ref}
                    </code>
                  ))}
                </p>
              ) : null}
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-slate-400">
          No messages yet in this session.
        </p>
      )}
      <div className="mt-4 border-t border-slate-800 pt-3">
        <AskPanel sessionId={session.id} projectId={session.project_id} />
      </div>
    </Card>
  );
}

async function TimelineSection({
  sessionId,
  projectId,
}: {
  sessionId: string;
  projectId: string;
}) {
  let timeline: Awaited<ReturnType<typeof api.getDebugTimeline>> | null = null;
  try {
    timeline = await api.getDebugTimeline(sessionId, projectId);
  } catch {
    timeline = null;
  }

  return (
    <Card
      title="Session timeline"
      subtitle="§38 — the audit trail: every analysis, message and tool call in order"
    >
      {timeline === null ? (
        <p className="text-sm text-slate-400">Timeline unavailable.</p>
      ) : timeline.items.length === 0 ? (
        <p className="text-sm text-slate-400">Nothing recorded yet.</p>
      ) : (
        <ol className="space-y-2">
          {timeline.items.map((event, index) => (
            <li key={`${event.at}-${index}`} className="flex gap-3 text-sm">
              <span className="w-36 shrink-0 font-mono text-xs text-slate-500">
                {formatDate(event.at)}
              </span>
              <span className="badge bg-slate-800 text-slate-400">
                {event.kind}
              </span>
              <span className="min-w-0 text-slate-300">
                {event.title}
                {event.detail ? (
                  <span className="block text-xs text-slate-500">
                    {event.detail}
                  </span>
                ) : null}
              </span>
            </li>
          ))}
        </ol>
      )}
      {timeline?.notes.length ? (
        <ul className="mt-3 space-y-1 text-xs text-slate-500">
          {timeline.notes.map((note) => (
            <li key={note}>· {note}</li>
          ))}
        </ul>
      ) : null}
    </Card>
  );
}
