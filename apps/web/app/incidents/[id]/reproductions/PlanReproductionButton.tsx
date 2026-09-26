'use client';

/**
 * Plan a reproduction experiment for an incident's hypothesis (§45, §47).
 *
 * Planning **never executes**: the backend only reads stored evidence here. The
 * button therefore says "Plan", and the engineer is taken to the workspace where
 * the safety confirmation gate lives. That separation is the phase's rule, not a
 * UI preference: a plan is something to review before anything runs.
 */

import { useRouter } from 'next/navigation';
import { useState } from 'react';

import { api, type ReproductionStrategy } from '@/lib/api-client';

export interface PlanCandidateOption {
  id: string;
  label: string;
  confidence: string;
  isPrimary: boolean;
}

const STRATEGIES: { value: ReproductionStrategy; label: string }[] = [
  { value: 'DEPENDENCY_FAULT', label: 'Dependency fault injection' },
  { value: 'SYNTHETIC_INPUT_REPLAY', label: 'Synthetic input replay' },
  { value: 'EVENT_REPLAY', label: 'Event replay' },
  { value: 'CONFIGURATION_REPLAY', label: 'Configuration replay' },
  { value: 'STATE_SNAPSHOT', label: 'State snapshot' },
];

export default function PlanReproductionButton({
  incidentId,
  projectId,
  candidates,
  defaultStrategy,
}: {
  incidentId: string;
  projectId?: string;
  candidates: PlanCandidateOption[];
  defaultStrategy?: ReproductionStrategy;
}) {
  const router = useRouter();
  const primary = candidates.find((item) => item.isPrimary) ?? candidates[0];
  const [candidateId, setCandidateId] = useState<string>(primary?.id ?? '');
  const [strategy, setStrategy] = useState<ReproductionStrategy>(
    defaultStrategy ?? 'DEPENDENCY_FAULT'
  );
  const [repetitions, setRepetitions] = useState(3);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const plan = async () => {
    setBusy(true);
    setError(null);
    try {
      const created = await api.createReproduction(incidentId, {
        projectId,
        payload: {
          candidate_id: candidateId || null,
          strategy,
          repetitions,
          requested_by: 'ui',
        },
      });
      router.push(`/reproductions/${created.experiment.id}`);
    } catch (cause: unknown) {
      setError(
        cause instanceof Error
          ? cause.message
          : 'The reproduction plan could not be created.'
      );
      setBusy(false);
    }
  };

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
        <label className="text-xs uppercase tracking-wider text-slate-500">
          Hypothesis
          <select
            className="input mt-1 w-full normal-case tracking-normal"
            value={candidateId}
            onChange={(event) => setCandidateId(event.target.value)}
          >
            {candidates.map((candidate) => (
              <option key={candidate.id} value={candidate.id}>
                {candidate.isPrimary ? '★ ' : ''}
                {candidate.label} ({candidate.confidence})
              </option>
            ))}
          </select>
        </label>

        <label className="text-xs uppercase tracking-wider text-slate-500">
          Strategy
          <select
            className="input mt-1 w-full normal-case tracking-normal"
            value={strategy}
            onChange={(event) =>
              setStrategy(event.target.value as ReproductionStrategy)
            }
          >
            {STRATEGIES.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
        </label>

        <label className="text-xs uppercase tracking-wider text-slate-500">
          Repetitions
          <select
            className="input mt-1 w-full normal-case tracking-normal"
            value={repetitions}
            onChange={(event) => setRepetitions(Number(event.target.value))}
          >
            {[1, 3, 5, 10].map((value) => (
              <option key={value} value={value}>
                {value} — {value === 1 ? 'single attempt' : 'repeat for determinism'}
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={plan}
          disabled={busy || candidates.length === 0}
          className="btn-primary"
        >
          {busy ? 'Planning…' : 'Plan experiment'}
        </button>
        <span className="text-xs text-slate-500">
          Planning only reads stored evidence. Nothing is executed until you
          confirm the sandbox on the workspace page.
        </span>
      </div>

      {error ? (
        <p className="text-xs text-argus-error">{error}</p>
      ) : null}
    </div>
  );
}
