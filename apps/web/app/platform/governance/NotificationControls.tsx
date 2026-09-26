'use client';

import { useRouter } from 'next/navigation';
import { useState } from 'react';

import { api, ApiError } from '@/lib/api-client';

/**
 * Mark a notification read or acknowledged (§55, §56).
 *
 * Reading and acknowledging are distinct, and this component keeps them
 * distinct: a reader can see a notification without owning it, and acknowledging
 * records a named actor. That is the difference between "someone looked" and
 * "someone is handling this".
 */
export default function NotificationControls({
  notificationId,
  projectId,
  status,
  created,
}: {
  notificationId: string;
  projectId: string;
  status: string;
  created: string;
}) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const act = async (acknowledge: boolean) => {
    setBusy(true);
    setError(null);
    try {
      await api.platformReadNotification({
        notificationId,
        projectId,
        actor: 'ui',
        acknowledge,
      });
      router.refresh();
    } catch (cause: unknown) {
      setError(cause instanceof ApiError ? cause.message : 'The change was rejected.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-slate-800/60 pt-3">
      <span className="text-xs text-slate-500">raised {created}</span>
      {status !== 'READ' ? (
        <button
          type="button"
          onClick={() => void act(false)}
          disabled={busy}
          className="rounded-md border border-slate-700 px-2 py-1 text-xs text-slate-300 hover:bg-slate-800 disabled:opacity-50"
        >
          Mark read
        </button>
      ) : null}
      {status !== 'ACKNOWLEDGED' ? (
        <button
          type="button"
          onClick={() => void act(true)}
          disabled={busy}
          className="rounded-md bg-argus-accent/20 px-2 py-1 text-xs font-medium text-argus-accent hover:bg-argus-accent/30 disabled:opacity-50"
        >
          Acknowledge
        </button>
      ) : null}
      {error ? <span className="text-xs text-argus-error">{error}</span> : null}
    </div>
  );
}
