'use client';

/**
 * The policy editor and the kill switch (§21, §34, §40).
 *
 * Two design rules come straight from the phase:
 *
 * 1. **The operator's ceilings are shown, not hidden.** `clamped` reports which
 *    values the process narrowed, and this component never hides that a stored
 *    value is not the effective one. Nothing here can raise a hard ceiling.
 * 2. **The emergency stop is one action and it is confirmed.** It is the only
 *    control that denies *everything* in a scope before any other rule runs, so
 *    it is presented as the weighty thing it is — and it asks for a name and a
 *    reason like every other authority decision.
 */

import { useRouter } from 'next/navigation';
import { useState } from 'react';

import {
  api,
  ApiError,
  type RemediationExecutionModeValue,
  type RemediationPolicy,
  type RemediationPolicyUpdate,
} from '@/lib/api-client';
import { announce, clearAnnouncement } from '@/lib/announce';
import { MODE_EXPLANATIONS } from '@/lib/remediation';

import Announcement from '@/app/components/Announcement';

//: Announcement scope, so this panel's confirmation cannot collide with another's.
const ANNOUNCE_SCOPE = 'remediation-policy';

const MODES: RemediationExecutionModeValue[] = [
  'OBSERVE_ONLY',
  'DRY_RUN',
  'SHADOW',
  'HUMAN_APPROVAL',
  'AUTONOMOUS',
  'EMERGENCY_STOP',
];

const RISKS = ['LOW', 'MEDIUM', 'HIGH', 'CRITICAL'] as const;

export default function PolicyEditor({
  projectId,
  policy,
}: {
  projectId: string;
  policy: RemediationPolicy;
}) {
  const router = useRouter();
  const [actor, setActor] = useState('');
  const [reason, setReason] = useState('');
  const [mode, setMode] = useState<RemediationExecutionModeValue>(
    policy.execution_mode
  );
  const [maxRisk, setMaxRisk] = useState(policy.autonomous_max_risk);
  const [cooldown, setCooldown] = useState(policy.cooldown_seconds);
  const [perWindow, setPerWindow] = useState(policy.max_actions_per_window);
  const [canary, setCanary] = useState(policy.canary_enabled);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const named = actor.trim().length > 0;

  async function run(work: () => Promise<string>) {
    setBusy(true);
    setError(null);
    clearAnnouncement(ANNOUNCE_SCOPE);
    try {
      // Recorded outside React: `router.refresh()` re-renders this component's
      // server parent, and a message held in component state did not survive it.
      announce(ANNOUNCE_SCOPE, await work());
      router.refresh();
    } catch (cause: unknown) {
      setError(
        cause instanceof ApiError || cause instanceof Error
          ? cause.message
          : 'unknown error'
      );
    } finally {
      setBusy(false);
    }
  }

  const save = () =>
    run(async () => {
      const payload: RemediationPolicyUpdate = {
        execution_mode: mode,
        autonomous_max_risk: maxRisk,
        cooldown_seconds: cooldown,
        max_actions_per_window: perWindow,
        canary_enabled: canary,
        updated_by: actor.trim(),
        notes: reason || null,
      };
      const updated = await api.updateRemediationPolicy(projectId, payload);
      const clamped =
        updated.clamped.length > 0
          ? ` Operator ceilings narrowed: ${updated.clamped.join(', ')}.`
          : '';
      return `Policy saved at revision ${updated.revision ?? '—'}.${clamped}`;
    });

  const stop = (engage: boolean) =>
    run(async () => {
      const updated = await api.setEmergencyStop(projectId, {
        engage,
        actor: actor.trim(),
        reason: reason || undefined,
      });
      return engage
        ? `Emergency stop engaged. Every action in this scope is denied before any other rule runs. (${updated.execution_mode})`
        : `Emergency stop released. The scope is back to ${updated.execution_mode}.`;
    });

  return (
    <div className="space-y-6">
      <section className="card space-y-4">
        <div>
          <h2 className="font-medium text-slate-200">Who decides</h2>
          <p className="mt-1 text-xs text-slate-500">
            {MODE_EXPLANATIONS[mode]}
          </p>
        </div>

        <div className="flex flex-wrap gap-3">
          <label className="text-xs text-slate-400">
            Your name
            <input
              className="mt-1 block w-56 rounded-md border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
              value={actor}
              onChange={(event) => setActor(event.target.value)}
              placeholder="on-call engineer"
            />
          </label>
          <label className="text-xs text-slate-400">
            Reason (recorded)
            <input
              className="mt-1 block w-80 rounded-md border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              placeholder="why this policy now"
            />
          </label>
        </div>

        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <label className="text-xs text-slate-400">
            Regime
            <select
              className="mt-1 block w-full rounded-md border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
              value={mode}
              onChange={(event) =>
                setMode(event.target.value as RemediationExecutionModeValue)
              }
            >
              {MODES.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </label>

          <label className="text-xs text-slate-400">
            Autonomous risk ceiling
            <select
              className="mt-1 block w-full rounded-md border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
              value={maxRisk}
              onChange={(event) =>
                setMaxRisk(
                  event.target.value as RemediationPolicy['autonomous_max_risk']
                )
              }
            >
              {RISKS.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </label>

          <label className="text-xs text-slate-400">
            Cooldown (seconds)
            <input
              type="number"
              min={0}
              className="mt-1 block w-full rounded-md border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
              value={cooldown}
              onChange={(event) => setCooldown(Number(event.target.value))}
            />
          </label>

          <label className="text-xs text-slate-400">
            Actions per window
            <input
              type="number"
              min={1}
              className="mt-1 block w-full rounded-md border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
              value={perWindow}
              onChange={(event) => setPerWindow(Number(event.target.value))}
            />
          </label>
        </div>

        <label className="flex items-center gap-2 text-sm text-slate-300">
          <input
            type="checkbox"
            checked={canary}
            onChange={(event) => setCanary(event.target.checked)}
          />
          Require a canary step first for actions that support one
        </label>

        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            className="btn-primary"
            disabled={busy || !named}
            title={named ? undefined : 'Record who changed the policy'}
            onClick={save}
          >
            Save policy
          </button>
          <button
            type="button"
            className="rounded-md bg-argus-error/20 px-3 py-1.5 text-sm font-medium text-argus-error hover:bg-argus-error/30 disabled:opacity-50"
            disabled={busy || !named}
            onClick={() => stop(true)}
          >
            Engage emergency stop
          </button>
          {policy.emergency_stop_active ? (
            <button
              type="button"
              className="rounded-md border border-slate-700 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-800 disabled:opacity-50"
              disabled={busy || !named}
              onClick={() => stop(false)}
            >
              Release emergency stop
            </button>
          ) : null}
        </div>

        {error ? <p className="text-xs text-argus-error">{error}</p> : null}
        <Announcement scope={ANNOUNCE_SCOPE} />
      </section>
    </div>
  );
}
