#!/usr/bin/env node
/**
 * Triage-aware npm audit gate.
 *
 * `npm audit --omit=dev` stays red as long as npm attributes the whole
 * next 9.x–16.x range to the installed 14.2.35 build, even though the fix
 * is installed and every advisory is triaged in docs/supply-chain-triage.md.
 * A dumb `--audit-level=high` gate would therefore be permanently red —
 * which trains everyone to ignore it.
 *
 * This gate instead fails only on advisories that are NOT accounted for:
 *
 *   1. run `npm audit --omit=dev --json`;
 *   2. collect every high/critical advisory id;
 *   3. fail on any id missing from scripts/audit-allowlist.json
 *      (the machine-checkable companion of the triage document);
 *   4. fail if the installed next is below the security-fix floor.
 *
 * Workflow when a new advisory appears:
 *   - fix it (bump the pin, verify the suite), or
 *   - triage it in docs/supply-chain-triage.md with applicability evidence
 *     and add its id to scripts/audit-allowlist.json.
 */

import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import path from "node:path";
import process from "node:process";

const root = path.resolve(process.cwd());
const allowlistPath = path.join(root, "scripts", "audit-allowlist.json");
const NEXT_FIX_FLOOR = { major: 14, minor: 2, patch: 25 };

function fail(message) {
  console.error(`::error::${message}`);
  process.exit(1);
}

function parseNextVersion(specifier) {
  const v = specifier.replace(/[^0-9.].*/, "");
  const [major, minor, patch] = v.split(".").map((p) => parseInt(p, 10));
  if ([major, minor, patch].some(Number.isNaN)) {
    fail(`cannot parse next version "${specifier}" from package.json`);
  }
  return { major, minor, patch };
}

const pkg = JSON.parse(readFileSync(path.join(root, "package.json"), "utf8"));
const nextVersion = parseNextVersion(pkg.dependencies?.next ?? "");
const atFloor =
  nextVersion.major > NEXT_FIX_FLOOR.major ||
  (nextVersion.major === NEXT_FIX_FLOOR.major &&
    (nextVersion.minor > NEXT_FIX_FLOOR.minor ||
      (nextVersion.minor === NEXT_FIX_FLOOR.minor &&
        nextVersion.patch >= NEXT_FIX_FLOOR.patch)));
if (!atFloor) {
  fail(
    `next ${pkg.dependencies.next} is below ${NEXT_FIX_FLOOR.major}.${NEXT_FIX_FLOOR.minor}.${NEXT_FIX_FLOOR.patch} (CVE-2025-29927). Bump it and run the suite.`,
  );
}

let audit;
try {
  const raw = execFileSync("npm", ["audit", "--omit=dev", "--json"], {
    cwd: root,
    encoding: "utf8",
    maxBuffer: 32 * 1024 * 1024,
  });
  audit = JSON.parse(raw);
} catch (error) {
  // npm audit exits non-zero when advisories exist; stdout still carries JSON.
  try {
    audit = JSON.parse(error.stdout);
  } catch {
    fail(`npm audit could not run: ${error.message}`);
  }
}

const allowlist = JSON.parse(readFileSync(allowlistPath, "utf8"));
const triaged = new Set(
  Object.entries(allowlist)
    .filter(([key]) => !key.startsWith("_"))
    .flatMap(([, ids]) => ids),
);

const untriaged = [];
const accounted = [];
for (const [name, info] of Object.entries(audit.vulnerabilities ?? {})) {
  for (const via of info.via ?? []) {
    if (typeof via !== "object" || via === null) continue;
    if (!["high", "critical"].includes(via.severity)) continue;
    if (triaged.has(via.source)) {
      accounted.push(`${name}#${via.source} (${via.severity})`);
    } else {
      untriaged.push(
        `${name}#${via.source} (${via.severity}): ${via.title}\n  ${via.url ?? ""}`,
      );
    }
  }
}

console.log(
  `next ${pkg.dependencies.next}: floor check passed (${NEXT_FIX_FLOOR.major}.${NEXT_FIX_FLOOR.minor}.${NEXT_FIX_FLOOR.patch}).`,
);
console.log(
  `high/critical production advisories: ${accounted.length + untriaged.length} (triaged: ${accounted.length}).`,
);

if (untriaged.length > 0) {
  console.error(
    `\n${untriaged.length} new high/critical advisor(ies) not in the triage record:\n\n` +
      untriaged.join("\n\n") +
      "\n\nFix the pin (run the full suite) or triage in docs/supply-chain-triage.md + scripts/audit-allowlist.json.",
  );
  process.exit(1);
}

console.log("Audit gate passed: every high/critical advisory is triaged.");
