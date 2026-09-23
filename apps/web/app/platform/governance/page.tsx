import { api, formatDate, type Project } from '@/lib/api';
import {
  formatRatio,
  PLATFORM_BOUNDARY,
  severityStyle,
  subsystemRequirementLabel,
} from '@/lib/platform';
import { Card, Empty, Facts, ProjectScope, Row, Table, readError } from '../ui';
import ConfigurationEditor from './ConfigurationEditor';
import NotificationControls from './NotificationControls';

export const metadata = {
  title: 'Governance',
};

export const dynamic = 'force-dynamic';

type SearchParams = { project_id?: string };

/**
 * The governance center (§91–§94, §53–§56, §100, §120).
 *
 * This is where the platform's own controls are visible and auditable:
 * versioned configuration with rollback, feature flags and their safe defaults,
 * notifications, and the webhook security requirements. Every sensitive setting
 * is shown with its version and author — an unversioned security control is
 * indistinguishable from an absent one.
 */
export default async function GovernancePage({ searchParams }: { searchParams: SearchParams }) {
  const requested =
    typeof searchParams.project_id === 'string' ? searchParams.project_id : '';

  let projects: Project[] = [];
  try {
    projects = (await api.listProjects(1, 50)).items;
  } catch {
    projects = [];
  }
  const project = projects.find((item) => item.id === requested) ?? projects[0];

  let error: string | null = null;
  const configuration = project
    ? await api.platformConfiguration(project.id).catch((reason: unknown) => {
        error = readError(reason);
        return null;
      })
    : null;
  const flags = project
    ? await api.platformFeatureFlags(project.id).catch(() => null)
    : null;
  const notifications = project
    ? await api.platformNotifications(project.id, { limit: 50 }).catch(() => null)
    : null;
  const integrations = await api.platformIntegrations().catch(() => null);
  const webhooks = await api.platformWebhookRequirements().catch(() => null);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-100">Governance</h1>
        <p className="mt-1 text-sm text-slate-400">
          Configuration, feature flags, notifications and integration security —
          versioned, auditable, and reversible.
        </p>
        <p className="mt-2 text-xs text-slate-500">{PLATFORM_BOUNDARY}</p>
      </div>

      <ProjectScope
        projects={projects}
        activeId={project?.id}
        basePath="/platform/governance"
      />

      {error ? (
        <Card title="Configuration unavailable">
          <Empty>{error}</Empty>
        </Card>
      ) : null}

      {project ? (
        <>
          <Card title="Feature flags" subtitle="§61, §62 — safe defaults">
            {flags ? (
              <>
                <Table
                  headers={['Flag', 'Enabled', 'Reason']}
                  rows={Object.entries(flags.flags).map(([flag, enabled]) => [
                    flag,
                    enabled ? (
                      <span key="e" className="badge bg-argus-success/15 text-argus-success">
                        Enabled
                      </span>
                    ) : (
                      <span key="e" className="badge bg-slate-800 text-slate-400">
                        Disabled
                      </span>
                    ),
                    flags.reasons[flag] ?? '—',
                  ])}
                />
                <p className="mt-3 text-xs text-slate-500">{flags.defaults}</p>
              </>
            ) : (
              <Empty>Feature flags could not be read.</Empty>
            )}
          </Card>

          <Card title="Configuration" subtitle="§91–§93 — versioned with provenance">
            {configuration ? (
              <ConfigurationEditor projectId={project.id} configuration={configuration} />
            ) : (
              <Empty>Configuration could not be read.</Empty>
            )}
          </Card>

          <Card title="Notifications" subtitle="§53–§56 — deduplicated, acknowledged">
            {!notifications || notifications.notifications.length === 0 ? (
              <Empty>No notification has been raised for this project.</Empty>
            ) : (
              <>
                <Facts data={notifications.summary} empty="No summary." />
                <div className="mt-3 space-y-3">
                  {notifications.notifications.map((item) => (
                    <div
                      key={item.id}
                      className="rounded-md border border-slate-800 bg-slate-900/40 p-3"
                    >
                      <div className="flex flex-wrap items-center gap-2">
                        <span className={`badge ${severityStyle(item.severity)}`}>
                          {item.severity}
                        </span>
                        <span className="badge bg-slate-800 text-slate-300">{item.kind}</span>
                        <h3 className="text-sm font-medium text-slate-200">{item.title}</h3>
                        <span className="text-xs text-slate-500">
                          {item.status} · seen {item.occurrence_count}×
                        </span>
                      </div>
                      {item.body ? (
                        <p className="mt-2 text-sm text-slate-400">{item.body}</p>
                      ) : null}
                      <NotificationControls
                        notificationId={item.id}
                        projectId={project.id}
                        status={item.status}
                        created={formatDate(item.created_at)}
                      />
                    </div>
                  ))}
                </div>
              </>
            )}
          </Card>
        </>
      ) : null}

      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Integrations" subtitle="§51, §52 — provider abstractions">
          {integrations ? (
            <Facts data={integrations.providers} empty="No provider is registered." />
          ) : (
            <Empty>Integrations could not be read.</Empty>
          )}
        </Card>

        <Card title="Webhook security" subtitle="§50, §100 — signed and replay-protected">
          {!webhooks ? (
            <Empty>Webhook requirements could not be read.</Empty>
          ) : (
            <>
              <div>
                {Object.entries(webhooks.headers).map(([header, meaning]) => (
                  <Row key={header} label={header}>
                    {meaning}
                  </Row>
                ))}
              </div>
              <div className="mt-3">
                <h3 className="mb-1 text-xs uppercase tracking-wider text-slate-500">
                  Requirements
                </h3>
                <ul className="space-y-1 text-xs text-slate-500">
                  {webhooks.requirements.map((item) => (
                    <li key={item}>· {item}</li>
                  ))}
                </ul>
              </div>
              <div className="mt-3">
                <h3 className="mb-1 text-xs uppercase tracking-wider text-slate-500">
                  Rejections
                </h3>
                <ul className="space-y-1 text-xs text-slate-500">
                  {webhooks.rejections.map((item) => (
                    <li key={item}>· {item}</li>
                  ))}
                </ul>
              </div>
            </>
          )}
        </Card>
      </div>
    </div>
  );
}
