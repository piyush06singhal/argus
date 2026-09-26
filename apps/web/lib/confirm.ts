/**
 * Confirmation copy for destructive actions (hardening W7).
 *
 * Why this is a module rather than a string in each component: a confirmation
 * prompt is the last thing a user reads before something irreversible happens,
 * and it has two jobs — say exactly what will happen, and say what will *not*
 * happen. Writing that per call site is how one of them ends up saying "Are you
 * sure?" and telling the user nothing.
 *
 * The rules encoded here:
 *
 * 1. **Name the object.** "Revoke this token" is forgettable; "Revoke 'ci-runner'"
 *    is checkable against what the user meant to click.
 * 2. **State the blast radius.** What stops working, and whether it can be undone.
 * 3. **Never imply reversibility that does not exist.** A revoked token cannot be
 *    un-revoked; a new one can be minted. Those are different sentences.
 *
 * The copy is data so it can be asserted in a test — the UI only renders it.
 */

export type DestructiveActionKind =
  | 'revoke_token'
  | 'delete_project'
  | 'engage_emergency_stop'
  | 'release_emergency_stop'
  | 'cancel_experiment';

export interface ConfirmationCopy {
  /** Short verb for the button that opens the confirmation. */
  action: string;
  /** The question the user answers, naming the object. */
  question: string;
  /** What happens if they go ahead. */
  consequence: string;
  /** Whether the effect can be taken back, said plainly. */
  reversibility: string;
  /** The label of the button that actually does it. */
  confirmLabel: string;
}

const COPY: Record<DestructiveActionKind, ConfirmationCopy> = {
  revoke_token: {
    action: 'Revoke',
    question: 'Revoke this token?',
    consequence:
      'Anything using it stops authenticating immediately — collectors, CI jobs and scripts fail on their next request.',
    reversibility:
      'A token cannot be un-revoked: the secret is stored only as a hash. You can mint a replacement, but every holder has to be updated.',
    confirmLabel: 'Revoke token',
  },
  delete_project: {
    action: 'Delete',
    question: 'Delete this project?',
    consequence:
      'Its environments, components, telemetry, incidents, analyses and learning history are removed with it.',
    reversibility:
      'This cannot be undone from the interface. Restoring it means restoring a database backup.',
    confirmLabel: 'Delete project',
  },
  engage_emergency_stop: {
    action: 'Engage emergency stop',
    question: 'Engage the emergency stop?',
    consequence:
      'No remediation action can be proposed, approved or executed, whatever the policy says.',
    reversibility:
      'Reversible at any time — releasing it restores the previous policy exactly as it was.',
    confirmLabel: 'Engage stop',
  },
  release_emergency_stop: {
    action: 'Release emergency stop',
    question: 'Release the emergency stop?',
    consequence:
      'Policy decides again: actions whose conditions are currently met may be authorized and executed without further prompting.',
    reversibility:
      'Reversible, but anything executed in the meantime is already done.',
    confirmLabel: 'Release stop',
  },
  cancel_experiment: {
    action: 'Cancel',
    question: 'Cancel this experiment?',
    consequence:
      'The run is stopped and its sandbox destroyed; partial telemetry is kept as evidence.',
    reversibility: 'Cancelling cannot be undone. Plan a new experiment instead.',
    confirmLabel: 'Cancel experiment',
  },
};

/**
 * The prompt for an action, optionally naming the object it affects.
 *
 * Naming is optional because some call sites already render the object beside
 * the button; those pass nothing and get the generic sentence rather than a
 * prompt interpolated with the string "undefined".
 */
export function confirmationFor(
  kind: DestructiveActionKind,
  subject?: string | null
): ConfirmationCopy {
  const copy = COPY[kind];
  if (!subject) {
    return copy;
  }
  return {
    ...copy,
    question: copy.question.replace(
      /^(.*?)(\?)$/,
      (_match, body: string, mark: string) =>
        `${body} — ${subject}${mark}`
    ),
  };
}

/** Every kind that must confirm before it acts. Used by the coverage test. */
export function requiresConfirmation(kind: DestructiveActionKind): boolean {
  return kind in COPY;
}

export const DESTRUCTIVE_ACTIONS = Object.keys(COPY) as DestructiveActionKind[];
