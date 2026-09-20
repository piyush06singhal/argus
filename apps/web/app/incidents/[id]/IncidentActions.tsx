'use client';

import { useRouter } from 'next/navigation';
import { useState } from 'react';

import { api, ApiError, type IncidentStatus } from '@/lib/api';
import { allowedIncidentTransitions, transitionLabel } from '@/lib/incidents';

/**
 * Lifecycle controls for an incident.
 *
 * The offered buttons come from the shared transition map, so the UI can never
 * present an action the API rejects with 409. Errors are surfaced verbatim
 * rather than swallowed — a rejected transition is information, not noise.
 */
export default function IncidentActions({
  incidentId,
  status,
}: {
  incidentId: string;
  status: IncidentStatus;
}) {
  const router = useRouter();
  const [actor, setActor] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState('');

  const run = async (target: IncidentStatus) => {
    setBusy(target);
    setError(null);
    try {
      const payload = { actor: actor.trim() || undefined };
      switch (target) {
        case 'ACKNOWLEDGED':
          await api.acknowledgeIncident(incidentId, payload.actor);
          break;
        case 'INVESTIGATING':
          await api.investigateIncident(incidentId, payload.actor);
          break;
        case 'MITIGATED':
          await api.mitigateIncident(incidentId, payload.actor);
          break;
        case 'RESOLVED':
          await api.resolveIncident(incidentId, payload.actor);
          break;
        case 'OPEN':
          await api.reopenIncident(incidentId, payload.actor);
          break;
        default:
          throw new Error(`Unsupported transition: ${target}`);
      }
      router.refresh();
    } catch (e) {
      setError(
        e instanceof ApiError ? e.message : 'Could not update the incident'
      );
    } finally {
      setBusy(null);
    }
  };

  const addNote = async () => {
    const title = note.trim();
    if (title === '') {
      return;
    }
    setBusy('NOTE');
    setError(null);
    try {
      await api.addIncidentNote(incidentId, title, actor.trim() || undefined);
      setNote('');
      router.refresh();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not add the note');
    } finally {
      setBusy(null);
    }
  };

  const transitions = allowedIncidentTransitions(status);

  return (
    <div className="card space-y-4">
      <div>
        <h2 className="font-medium text-slate-200">Lifecycle</h2>
        <p className="mt-1 text-xs text-slate-500">
          Every change is recorded on the timeline with its actor.
        </p>
      </div>

      <div className="flex flex-col gap-2 sm:flex-row sm:items-end">
        <div className="flex flex-1 flex-col gap-1">
          <label
            htmlFor="actor"
            className="text-xs font-medium text-slate-400"
          >
            Actor (recorded on the timeline)
          </label>
          <input
            id="actor"
            className="input"
            placeholder="oncall engineer"
            value={actor}
            onChange={(event) => setActor(event.target.value)}
          />
        </div>
      </div>

      <div className="flex flex-wrap gap-2">
        {transitions.length === 0 ? (
          <p className="text-sm text-slate-500">
            This incident has no available transitions.
          </p>
        ) : (
          transitions.map((target) => (
            <button
              key={target}
              type="button"
              className={target === 'OPEN' ? 'btn-ghost' : 'btn-primary'}
              onClick={() => run(target)}
              disabled={busy !== null}
            >
              {busy === target ? 'Working…' : transitionLabel(target)}
            </button>
          ))
        )}
      </div>

      <div className="border-t border-slate-800 pt-4">
        <label htmlFor="note" className="text-xs font-medium text-slate-400">
          Add an investigation note
        </label>
        <div className="mt-2 flex flex-col gap-2 sm:flex-row">
          <input
            id="note"
            className="input flex-1"
            placeholder="Mitigation applied at the edge…"
            value={note}
            onChange={(event) => setNote(event.target.value)}
          />
          <button
            type="button"
            className="btn-ghost"
            onClick={addNote}
            disabled={busy !== null || note.trim() === ''}
          >
            {busy === 'NOTE' ? 'Saving…' : 'Add note'}
          </button>
        </div>
      </div>

      {error ? (
        <p className="text-sm text-argus-error" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}
