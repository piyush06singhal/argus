'use client';

import { useRouter } from 'next/navigation';
import { useState } from 'react';

import { api, ApiError, formatDate, type ConfigurationResponse } from '@/lib/api-client';
import { announce, clearAnnouncement } from '@/lib/announce';

import Announcement from '@/app/components/Announcement';

//: Announcement scope, so this panel's confirmation cannot collide with another's.
const ANNOUNCE_SCOPE = 'platform-configuration';

/**
 * Versioned configuration editing (§91–§94).
 *
 * Two rules: a write must name its reason (an unexplained change to a safety
 * setting is not auditable), and rollback restores a previous *version* rather
 * than re-typing settings, so the platform's own Phase 9 mechanisms carry the
 * restore. The settings are edited as JSON so the shape is explicit; a bad shape
 * is rejected by the backend and surfaced verbatim.
 *
 * The scope list is the set the platform *owns*. The editor used to offer
 * ``PROJECT``, ``ENVIRONMENT`` and ``PLATFORM``, none of which are writable —
 * those are reported from environment configuration — so every save from this
 * form was refused. The scopes below are the ones a write is accepted for.
 */
const WRITABLE_SCOPES = [
  { value: 'PROJECT_SETTINGS', label: 'Project settings' },
  { value: 'SLO', label: 'Service-level objectives' },
  { value: 'NOTIFICATIONS', label: 'Notifications' },
  { value: 'LEARNING', label: 'Learning' },
  { value: 'RETENTION', label: 'Retention' },
];
export default function ConfigurationEditor({
  projectId,
  configuration,
}: {
  projectId: string;
  configuration: ConfigurationResponse;
}) {
  const router = useRouter();
  const [scope, setScope] = useState(WRITABLE_SCOPES[0].value);
  const [settings, setSettings] = useState(
    JSON.stringify(configuration.overrides ?? {}, null, 2)
  );
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = async () => {
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(settings);
    } catch {
      setError('Settings must be valid JSON.');
      return;
    }
    if (!reason.trim()) {
      setError('A reason is required — an unexplained change is not auditable.');
      return;
    }
    setBusy(true);
    setError(null);
    clearAnnouncement(ANNOUNCE_SCOPE);
    try {
      await api.platformWriteConfiguration(projectId, {
        scope,
        settings: parsed,
        change_summary: reason.trim(),
        reason: reason.trim(),
        actor: 'ui',
      });
      announce(ANNOUNCE_SCOPE, 'Configuration saved as a new version.');
      setReason('');
      router.refresh();
    } catch (cause: unknown) {
      setError(cause instanceof ApiError ? cause.message : 'The change was rejected.');
    } finally {
      setBusy(false);
    }
  };

  const rollback = async (targetVersion: number) => {
    setBusy(true);
    setError(null);
    clearAnnouncement(ANNOUNCE_SCOPE);
    try {
      await api.platformRollbackConfiguration(projectId, {
        scope,
        target_version: targetVersion,
        actor: 'ui',
        reason: `rolled back via UI to version ${targetVersion}`,
      });
      announce(ANNOUNCE_SCOPE, `Rolled back to version ${targetVersion}.`);
      router.refresh();
    } catch (cause: unknown) {
      setError(cause instanceof ApiError ? cause.message : 'The rollback was rejected.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-4">
      <div>
        {Object.entries(configuration.sections).map(([section, content]) => (
          <div key={section} className="border-b border-slate-800/60 py-2 last:border-0">
            <h3 className="text-xs uppercase tracking-wider text-slate-500">{section}</h3>
            {content && typeof content === 'object' ? (
              <pre className="mt-1 overflow-x-auto text-xs text-slate-300">
                {JSON.stringify(content, null, 2)}
              </pre>
            ) : (
              <p className="mt-1 text-sm text-slate-300">{String(content)}</p>
            )}
          </div>
        ))}
      </div>

      {configuration.redacted_fields.length > 0 ? (
        <p className="text-xs text-slate-500">
          Redacted: {configuration.redacted_fields.join(', ')} (§92, §100)
        </p>
      ) : null}

      <div className="border-t border-slate-800 pt-4">
        <h3 className="mb-2 text-xs uppercase tracking-wider text-slate-500">
          Write a new version
        </h3>
        <div className="grid gap-2">
          <select className="input" value={scope} onChange={(e) => setScope(e.target.value)} aria-label="Configuration scope">
            {WRITABLE_SCOPES.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
          <textarea
            className="input font-mono text-xs"
            rows={10}
            value={settings}
            onChange={(e) => setSettings(e.target.value)}
            aria-label="Configuration JSON"
          />
          <input
            className="input"
            placeholder="Reason (required, recorded)"
            aria-label="Reason for this configuration change"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
        </div>
        <button
          type="button"
          onClick={() => void save()}
          disabled={busy}
          className="mt-3 rounded-md bg-argus-accent/20 px-3 py-1.5 text-sm font-medium text-argus-accent hover:bg-argus-accent/30 disabled:opacity-50"
        >
          {busy ? 'Saving…' : 'Save new version'}
        </button>
        <Announcement scope={ANNOUNCE_SCOPE} />
        {error ? <p className="mt-2 text-xs text-argus-error">{error}</p> : null}
      </div>

      <div className="border-t border-slate-800 pt-4">
        <h3 className="mb-2 text-xs uppercase tracking-wider text-slate-500">
          Versions
        </h3>
        {configuration.versions.length === 0 ? (
          <p className="text-sm text-slate-400">No version has been recorded yet.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[480px] text-left text-sm">
              <thead>
                <tr className="border-b border-slate-800 text-xs uppercase tracking-wider text-slate-500">
                  <th className="py-2 pr-4 font-medium">Version</th>
                  <th className="py-2 pr-4 font-medium">Scope</th>
                  <th className="py-2 pr-4 font-medium">By</th>
                  <th className="py-2 pr-4 font-medium">Change</th>
                  <th className="py-2 pr-4 font-medium">When</th>
                  <th className="py-2 pr-4 font-medium" />
                </tr>
              </thead>
              <tbody>
                {configuration.versions.map((version) => (
                  <tr key={version.id} className="border-b border-slate-800/60">
                    <td className="py-2 pr-4 text-slate-300">{version.version}</td>
                    <td className="py-2 pr-4 text-slate-300">{version.scope}</td>
                    <td className="py-2 pr-4 text-slate-400">{version.changed_by ?? '—'}</td>
                    <td className="py-2 pr-4 text-slate-400">
                      {version.change_summary ?? '—'}
                      {version.rolled_back_from != null
                        ? ` (from v${version.rolled_back_from})`
                        : ''}
                    </td>
                    <td className="py-2 pr-4 text-slate-400">
                      {formatDate(version.created_at)}
                    </td>
                    <td className="py-2 pr-4">
                      <button
                        type="button"
                        onClick={() => void rollback(version.version)}
                        disabled={busy}
                        className="rounded-md border border-slate-700 px-2 py-1 text-xs text-slate-300 hover:bg-slate-800 disabled:opacity-50"
                      >
                        Restore
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
