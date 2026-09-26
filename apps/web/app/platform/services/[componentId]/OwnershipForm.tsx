'use client';

import { useRouter } from 'next/navigation';
import { useState } from 'react';

import { api, ApiError } from '@/lib/api-client';

/**
 * Record ownership for a service (§31, §92).
 *
 * Ownership is recorded by a human, never inferred. The form is explicit that
 * this is a write: it submits a whole ownership record and the backend versions
 * and audits the change. An empty field is sent as "not recorded", so clearing a
 * field never silently keeps the old value.
 */
export default function OwnershipForm({
  componentId,
  projectId,
  current,
  unknownLabel,
}: {
  componentId: string;
  projectId: string;
  current: Record<string, unknown>;
  unknownLabel: string;
}) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [team, setTeam] = useState(
    typeof current.team === 'string' && current.team !== unknownLabel
      ? current.team
      : ''
  );
  const [ownerName, setOwnerName] = useState(
    typeof current.owner_name === 'string' ? current.owner_name : ''
  );
  const [contactEmail, setContactEmail] = useState(
    typeof current.contact_email === 'string' ? current.contact_email : ''
  );
  const [onCall, setOnCall] = useState(
    typeof current.on_call === 'string' ? current.on_call : ''
  );
  const [documentationUrl, setDocumentationUrl] = useState(
    typeof current.documentation_url === 'string' ? current.documentation_url : ''
  );

  const submit = async () => {
    if (!team.trim()) {
      setError('A team is required — ownership cannot be recorded without one.');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.platformSetOwnership({
        componentId,
        projectId,
        team: team.trim(),
        ownerName: ownerName.trim() || undefined,
        contactEmail: contactEmail.trim() || undefined,
        onCall: onCall.trim() || undefined,
        documentationUrl: documentationUrl.trim() || undefined,
        actor: 'ui',
      });
      setOpen(false);
      router.refresh();
    } catch (cause: unknown) {
      setError(
        cause instanceof ApiError ? cause.message : 'Ownership could not be saved.'
      );
    } finally {
      setBusy(false);
    }
  };

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="mt-3 rounded-md border border-slate-700 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-800"
      >
        Record ownership
      </button>
    );
  }

  return (
    <div className="mt-4 space-y-2 border-t border-slate-800 pt-4">
      <input className="input" placeholder="Team (required)" aria-label="Owning team" value={team} onChange={(e) => setTeam(e.target.value)} />
      <input className="input" placeholder="Owner name" aria-label="Owner name" value={ownerName} onChange={(e) => setOwnerName(e.target.value)} />
      <input className="input" placeholder="Contact email" aria-label="Contact email" value={contactEmail} onChange={(e) => setContactEmail(e.target.value)} />
      <input className="input" placeholder="On-call" aria-label="On-call rotation" value={onCall} onChange={(e) => setOnCall(e.target.value)} />
      <input className="input" placeholder="Documentation URL" aria-label="Documentation URL" value={documentationUrl} onChange={(e) => setDocumentationUrl(e.target.value)} />
      <div className="flex gap-2">
        <button
          type="button"
          onClick={() => void submit()}
          disabled={busy}
          className="rounded-md bg-argus-accent/20 px-3 py-1.5 text-sm font-medium text-argus-accent hover:bg-argus-accent/30 disabled:opacity-50"
        >
          {busy ? 'Saving…' : 'Save ownership'}
        </button>
        <button
          type="button"
          onClick={() => setOpen(false)}
          className="rounded-md border border-slate-700 px-3 py-1.5 text-sm text-slate-300 hover:bg-slate-800"
        >
          Cancel
        </button>
      </div>
      {error ? <p className="text-xs text-argus-error">{error}</p> : null}
    </div>
  );
}
