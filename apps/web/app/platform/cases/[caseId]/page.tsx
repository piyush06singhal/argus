import Link from 'next/link';

import { api, formatDate, type CaseDetailResponse, type Project } from '@/lib/api';
import {
  caseStatusLabel,
  caseStatusStyle,
  isCaseTerminal,
  PLATFORM_BOUNDARY,
} from '@/lib/platform';
import { Card, Empty, Facts, Table, readError } from '../../ui';
import CaseControls from './CaseControls';
import CaseAssistant from './CaseAssistant';

export const metadata = {
  title: 'Reliability Case',
};

export const dynamic = 'force-dynamic';

/**
 * The Reliability Case workspace (§114, §116, §21).
 *
 * This is the phase's central artefact: one page that follows a single
 * situation through every stage. Two rules shape it:
 *
 * 1. **The timeline is the case.** Evidence, actions and results are interleaved
 *    in the order they occurred, because a workspace that separates "what we
 *    decided" from "what happened" makes causation look cleaner than it was.
 * 2. **Controls never bypass Phase 9.** Status transitions are offered only from
 *    `allowed_transitions`, which come from the backend state machine, and the
 *    assistant cites stored rows and says when it does not know.
 */
export default async function CaseWorkspacePage({
  params,
  searchParams,
}: {
  params: { caseId: string };
  searchParams: { project_id?: string };
}) {
  const requested =
    typeof searchParams.project_id === 'string' ? searchParams.project_id : '';

  let projects: Project[] = [];
  try {
    projects = (await api.listProjects(1, 50)).items;
  } catch {
    projects = [];
  }
  const project = projects.find((item) => item.id === requested) ?? projects[0];

  if (!project) {
    return (
      <Card title="No projects">
        <Empty>Every case read is scoped to a project.</Empty>
      </Card>
    );
  }

  let detail: CaseDetailResponse | null = null;
  let error: string | null = null;
  try {
    detail = await api.platformCase(params.caseId, project.id, true);
  } catch (reason) {
    error = readError(reason);
  }

  if (error || !detail) {
    return (
      <Card title="Case unavailable">
        <Empty>{error ?? 'The case could not be loaded.'}</Empty>
        <Link href={`/platform/cases?project_id=${project.id}`} className="mt-3 inline-block text-xs text-argus-accent">
          ← Back to cases
        </Link>
      </Card>
    );
  }

  const record = detail.case;
  const evidence = detail.evidence ?? {};
  const notes = Array.isArray(detail.notes) ? detail.notes : [];
  const workflows = Array.isArray(detail.workflows) ? detail.workflows : [];

  return (
    <div className="space-y-6">
      <div>
        <Link
          href={`/platform/cases?project_id=${project.id}`}
          className="text-xs text-argus-accent"
        >
          ← All cases
        </Link>
        <div className="mt-2 flex flex-wrap items-center gap-3">
          <h1 className="text-2xl font-semibold text-slate-100">{record.title}</h1>
          <span className={`badge ${caseStatusStyle(record.status)}`}>
            {caseStatusLabel(record.status)}
          </span>
          {record.severity ? (
            <span className="badge bg-slate-800 text-slate-300">{record.severity}</span>
          ) : null}
        </div>
        <p className="mt-1 font-mono text-xs text-slate-500">{record.reference}</p>
        {record.summary ? (
          <p className="mt-2 text-sm text-slate-400">{record.summary}</p>
        ) : null}
        <p className="mt-2 text-xs text-slate-500">{PLATFORM_BOUNDARY}</p>
      </div>

      <Card title="Case facts">
        <div className="grid gap-4 lg:grid-cols-2">
          <Facts
            data={{
              trigger: record.trigger,
              opened_at: formatDate(record.opened_at),
              opened_by: record.opened_by ?? '—',
              closed_at: record.closed_at ? formatDate(record.closed_at) : null,
              duration: record.duration_seconds != null
                ? `${Math.round(record.duration_seconds / 60)} minutes`
                : null,
              timeline_entries: record.timeline_entries,
              primary_component: record.primary_component_id ?? '—',
              incident: record.incident_id ?? '—',
            }}
          />
          <div>
            <h3 className="mb-2 text-xs uppercase tracking-wider text-slate-500">
              Components
            </h3>
            {record.component_ids.length === 0 ? (
              <Empty>No component is associated with this case.</Empty>
            ) : (
              <div className="flex flex-wrap gap-2">
                {record.component_ids.map((id) => (
                  <Link
                    key={id}
                    href={`/platform/services/${encodeURIComponent(id)}?project_id=${project.id}`}
                    className="badge bg-slate-800 font-mono text-xs text-slate-300"
                  >
                    {id.slice(0, 8)}
                  </Link>
                ))}
              </div>
            )}
          </div>
        </div>
      </Card>

      <CaseControls
        caseId={record.id}
        projectId={project.id}
        status={record.status}
        allowedTransitions={record.allowed_transitions}
        terminal={isCaseTerminal(record.status)}
      />

      <Card title="Case timeline" subtitle="Evidence, decisions and results in order (§21)">
        {detail.timeline.length === 0 ? (
          <Empty>No timeline entry has been recorded for this case.</Empty>
        ) : (
          <Table
            headers={['#', 'When', 'Event', 'Detail', 'Source', 'Actor']}
            rows={detail.timeline.map((entry) => [
              String(entry.sequence),
              formatDate(entry.occurred_at),
              <span key="t">
                {entry.title}
                {entry.result ? (
                  <span className="ml-2 text-xs text-slate-500">{entry.result}</span>
                ) : null}
              </span>,
              entry.detail ?? '—',
              <span key="s" className="text-xs text-slate-500">
                {entry.source}
                {entry.system_action ? ' · system' : ''}
              </span>,
              entry.actor ?? '—',
            ])}
          />
        )}
      </Card>

      <CaseAssistant caseId={record.id} projectId={project.id} />

      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Workflow stages" subtitle="§11 — the stage machine over this case">
          {workflows.length === 0 ? (
            <Empty>No workflow has been started for this case.</Empty>
          ) : (
            <Table
              headers={['Stage', 'Status', 'Started', 'Completed']}
              rows={workflows.map((item) => [
                String(item.stage ?? item.name ?? '—'),
                String(item.status ?? '—'),
                formatDate(
                  typeof item.started_at === 'string' ? item.started_at : null
                ),
                formatDate(
                  typeof item.completed_at === 'string' ? item.completed_at : null
                ),
              ])}
            />
          )}
        </Card>

        <Card title="Case state" subtitle="§7 — the context this case was opened against">
          <Facts data={detail.state} empty="No context snapshot is bound to this case." />
        </Card>
      </div>

      <Card title="Evidence" subtitle="The stored rows the case reasoned over">
        <Facts data={evidence} empty="No evidence has been attached yet." />
      </Card>

      {notes.length > 0 ? (
        <Card title="Notes">
          <ul className="space-y-2 text-sm text-slate-300">
            {notes.map((note, index) => (
              <li key={index}>· {note}</li>
            ))}
          </ul>
        </Card>
      ) : null}
    </div>
  );
}
