import Link from 'next/link';

import { api, formatDate, type ActivityResponse, type Project } from '@/lib/api';
import { activityEventLabel, PLATFORM_BOUNDARY } from '@/lib/platform';
import { Card, Empty, ProjectScope, Table, readError } from '../ui';

export const metadata = {
  title: 'Activity',
};

export const dynamic = 'force-dynamic';

type SearchParams = { project_id?: string; event_type?: string; offset?: string };

/**
 * The ARGUS activity feed (§25, §71).
 *
 * Every row is a stored event with its correlation id, so a reader can follow
 * one situation from the API through the worker to the remediation. Events are
 * shown with their processed flag: an event that has not been processed is
 * information about ARGUS's own state, not noise to hide.
 */
export default async function ActivityPage({ searchParams }: { searchParams: SearchParams }) {
  const requested =
    typeof searchParams.project_id === 'string' ? searchParams.project_id : '';
  const eventType =
    typeof searchParams.event_type === 'string' ? searchParams.event_type : '';
  const offset = Number.parseInt(String(searchParams.offset ?? '0'), 10) || 0;

  let projects: Project[] = [];
  try {
    projects = (await api.listProjects(1, 50)).items;
  } catch {
    projects = [];
  }
  const project = projects.find((item) => item.id === requested) ?? projects[0];

  let feed: ActivityResponse | null = null;
  let error: string | null = null;
  if (project) {
    try {
      feed = await api.platformActivity(project.id, {
        eventType: eventType || undefined,
        limit: 100,
        offset,
      });
    } catch (reason) {
      error = readError(reason);
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-100">Activity</h1>
        <p className="mt-1 text-sm text-slate-400">
          Every significant ARGUS event, correlated across the platform.
        </p>
        <p className="mt-2 text-xs text-slate-500">{PLATFORM_BOUNDARY}</p>
      </div>

      <ProjectScope
        projects={projects}
        activeId={project?.id}
        basePath="/platform/activity"
      />

      {error ? (
        <Card title="Activity unavailable">
          <Empty>{error}</Empty>
        </Card>
      ) : !project ? (
        <Card title="No projects">
          <Empty>The activity feed is project-scoped.</Empty>
        </Card>
      ) : (
        <>
          <Card title="Filter" subtitle="Platform event types (§25)">
            <form method="get" className="flex flex-wrap gap-2">
              <input type="hidden" name="project_id" value={project.id} />
              <select className="input" name="event_type" defaultValue={eventType} aria-label="Filter by event type">
                <option value="">All events</option>
                {[
                  'COMPONENT_STATE_CHANGED',
                  'ANOMALY_DETECTED',
                  'INCIDENT_CREATED',
                  'RCA_COMPLETED',
                  'REPRODUCTION_COMPLETED',
                  'PATCH_VERIFIED',
                  'FORECAST_GENERATED',
                  'REMEDIATION_PROPOSED',
                  'REMEDIATION_COMPLETED',
                  'REMEDIATION_ROLLED_BACK',
                  'LEARNING_COMPLETED',
                  'CASE_OPENED',
                ].map((value) => (
                  <option key={value} value={value}>
                    {activityEventLabel(value)}
                  </option>
                ))}
              </select>
              <button type="submit" className="btn btn-primary">
                Apply
              </button>
            </form>
          </Card>

          <Card title="Feed" subtitle={`${feed?.items.length ?? 0} event(s)`}>
            {!feed || feed.items.length === 0 ? (
              <Empty>No platform event has been recorded in this window.</Empty>
            ) : (
              <Table
                headers={['When', 'Event', 'Title', 'Source', 'Subject', 'Processed']}
                rows={feed.items.map((item) => [
                  formatDate(item.occurred_at),
                  activityEventLabel(item.event_type),
                  item.link ? (
                    <Link key="t" href={item.link} className="text-argus-accent">
                      {item.title}
                    </Link>
                  ) : (
                    item.title
                  ),
                  item.source,
                  item.subject_type && item.subject_id
                    ? `${item.subject_type} ${item.subject_id.slice(0, 8)}`
                    : '—',
                  item.processed ? 'Yes' : 'Pending',
                ])}
              />
            )}
            <p className="mt-3 text-xs text-slate-500">
              Showing {feed?.offset ?? 0}–{(feed?.offset ?? 0) + (feed?.items.length ?? 0)}
              {feed ? ` · limit ${feed.limit}` : ''}
            </p>
          </Card>
        </>
      )}
    </div>
  );
}
