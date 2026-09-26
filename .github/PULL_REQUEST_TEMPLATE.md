<!-- Keep PRs to one logical change. See CONTRIBUTING.md for the rules. -->

## What does this PR change?

One or two sentences: the problem and the fix/feature.

## Why this way?

Briefly: alternatives considered, and why this one is right for ARGUS
(deterministic engines, evidence-grounded output, honest failure states).

## Checklist

- [ ] Tests added or updated (bug fixes ship with a failing-without-fix test)
- [ ] `ruff check` + `ruff format --check` + `mypy app` pass (backend)
- [ ] `npx tsc --noEmit` + `npm test` + `npm run build` pass (web)
- [ ] Live gate(s) re-run if engine behaviour changed — name them:
- [ ] Docs updated (`README.md`, `docs/`, `CHANGELOG.md` under Unreleased)
- [ ] No new dependencies without justification (size, license, maintenance)
