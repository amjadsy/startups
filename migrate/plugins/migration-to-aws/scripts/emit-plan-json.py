#!/usr/bin/env python3
"""Emit plan.json from a finished migration run.

Copies already-validated values from a run's artifacts into a `plan.json`
summary the AWS Startups Migrate web import page ingests. It GENERATES nothing:
every field is copied from a validated artifact or omitted, because every extra
field is a mistake surface.

Fail-open by design: any missing or unreadable input leaves the migration
successful, writes no file, prints the reason, and stays re-runnable. A missed
handoff must never cost a customer their migration result.

Scope: GCP and Heroku infra runs (the two skills that persist a cost estimate).
The OpenAI/LLM-to-Bedrock path has no persisted cost artifact and is a follow-up.

Usage:
  python3 scripts/emit-plan-json.py --migration-dir <dir>

Reads:
  <dir>/.phase-status.json              run_id, owning_skill
  <dir>/estimation-infra.json           projected_costs.aws_monthly_balanced, current_costs.*
  <PLUGIN_ROOT>/.claude-plugin/plugin.json   version

Writes:
  <dir>/plan.json

Status line (stdout, machine-readable):
  PLAN_OK   | path=<dir>/plan.json | platform=GCP | scope=INFRA_ONLY
  PLAN_SKIP | reason=<why>                 (fail-open; exit 0, no file written)
Exit code is 0 for success AND for every fail-open skip; only a usage error is non-zero.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

# scripts/ -> plugin root (holds .claude-plugin/plugin.json).
PLUGIN_ROOT = Path(__file__).resolve().parent.parent

# Highest plan.json schema the web import page understands (kept in lock-step there).
SCHEMA_VERSION = 1

# owning_skill (telemetry id written to .phase-status.json at _init) ->
# sourcePlatform. Not generated data: each skill records its own id at _init and
# never changes it. LLM_TO_BEDROCK (OpenAI) is intentionally absent — it has no
# persisted cost artifact to copy, so it is a follow-up.
SKILL_TO_PLATFORM = {
    "GCP_TO_AWS": "GCP",
    "HEROKU_TO_AWS": "HEROKU",
}

# We copy the "balanced" AWS scenario (projected_costs.aws_monthly_balanced), so the
# basis the web contract records for that figure is BALANCED.
AWS_MONTHLY_BASIS = "BALANCED"

# The current-cost key each platform writes. Checked first so an extra *_monthly
# field in current_costs can't be copied into sourceMonthly by accident.
PLATFORM_SOURCE_KEY = {
    "GCP": "gcp_monthly",
    "HEROKU": "heroku_monthly",
}


class SkipEmit(Exception):
    """Fail-open signal: a reason to skip writing plan.json without failing the run."""


def _load_json(path: Path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _is_amount(value: object) -> bool:
    """True for a real, finite, non-negative JSON number.

    Excludes bool (a Python int subclass, so a JSON `true` would otherwise slip
    through) and non-finite floats: json.load accepts Infinity/NaN by default, and
    json.dumps would then emit the literal tokens `Infinity`/`NaN`, which are not
    valid JSON and the strict web import rejects — sinking the whole handoff.
    """
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _source_monthly(current_costs: object, source_platform: str) -> float | None:
    """The numeric monthly source-platform cost if present, else None.

    `current_costs` carries a skill-specific key (e.g. gcp_monthly / heroku_monthly)
    and may instead mark the baseline unavailable (no billing access), in which case
    there is no number to copy and sourceMonthly is omitted. The platform's own key
    is preferred so an unrelated *_monthly field can't be copied by accident.
    """
    if not isinstance(current_costs, dict):
        return None
    # Honor ONLY the platform's own key. No fallback to any other *_monthly field:
    # copying an unrelated one (e.g. support_monthly) would fabricate a source figure.
    preferred = PLATFORM_SOURCE_KEY.get(source_platform)
    if preferred and _is_amount(current_costs.get(preferred)):
        return current_costs[preferred]
    return None


def _service_items(projected: object) -> list[dict]:
    """Per-service line items copied from projected_costs.breakdown.

    The breakdown is keyed by service, but its shape varies across skills: a key is
    either the display name itself (e.g. "Elastic Beanstalk") or a slug carrying a
    nested "service" label (e.g. "security_baseline" -> "AWS Security Baseline").
    `.mid` is the monthly figure. The "total" rollup row and any entry without a
    usable numeric `.mid` are skipped, and GCP runs may carry an empty breakdown.

    classification is INFRASTRUCTURE — always correct here because this writer only
    emits INFRA_ONLY runs (AI-inclusive runs are skipped upstream), so it is implied
    by the scope, not generated. `category` is not persisted, so it is omitted.
    """
    if not isinstance(projected, dict):
        return []
    breakdown = projected.get("breakdown")
    if not isinstance(breakdown, dict):
        return []

    items: list[dict] = []
    for key, entry in breakdown.items():
        # Drop the aggregate row (any casing) so the total is never shown as a service.
        if not isinstance(key, str) or key.strip().lower() == "total" or not isinstance(entry, dict):
            continue
        monthly = entry.get("mid")
        if not _is_amount(monthly):
            continue
        name = entry.get("service")
        # Fall back to the key when the label is missing OR blank, so a whitespace-only
        # label doesn't discard an item whose key is a perfectly good display name.
        if not isinstance(name, str) or not name.strip():
            name = key
        # Skip if even the key is blank, and skip an aggregate that surfaced via the
        # label rather than the key (e.g. service:"Total") so a rollup is never shown.
        if not name.strip() or name.strip().lower() == "total":
            continue
        items.append(
            {
                "serviceName": name,
                "monthlyCost": monthly,
                "classification": "INFRASTRUCTURE",
            }
        )
    return items


def build_plan(migration_dir: Path, plugin_json_path: Path) -> tuple[dict, str, str]:
    """Build the plan dict from validated artifacts, or raise SkipEmit to fail open."""
    status_path = migration_dir / ".phase-status.json"
    infra_path = migration_dir / "estimation-infra.json"

    if not status_path.is_file():
        raise SkipEmit("no .phase-status.json in migration dir")
    if not infra_path.is_file():
        raise SkipEmit("no estimation-infra.json (no cost estimate to hand off)")

    status = _load_json(status_path)
    if not isinstance(status, dict):
        raise SkipEmit(".phase-status.json is not a JSON object")

    owning_skill = status.get("owning_skill")
    # Guard the type before the dict lookup: a non-string (e.g. a list) is unhashable
    # and would raise TypeError, and only a string can name a skill anyway.
    source_platform = SKILL_TO_PLATFORM.get(owning_skill) if isinstance(owning_skill, str) else None
    if source_platform is None:
        raise SkipEmit(f"owning_skill {owning_skill!r} has no web handoff yet")

    # Scope reflects what the run actually costed. A run that also costed AI
    # (estimation-ai.json present) is FULL or AI_ONLY, and its awsMonthly must fold
    # in the Bedrock cost under a *_PLUS_AI_SUM basis — that merge is a follow-up.
    # This writer emits only the pure-infra case, so skip any AI-inclusive run
    # rather than mislabel it INFRA_ONLY with an infra-only cost.
    if (migration_dir / "estimation-ai.json").is_file():
        raise SkipEmit("AI-inclusive run (FULL/AI_ONLY) — infra-only handoff is a follow-up")
    scope = "INFRA_ONLY"

    # runId carries the attribution the handoff exists for; without it there is
    # nothing to hand off, so fail open rather than write an unattributable plan.
    # Must be a non-empty string: a numeric/other run_id would be copied verbatim and
    # rejected by the strict web schema (which types runId as a string).
    run_id = status.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise SkipEmit("no usable run_id in .phase-status.json")

    infra = _load_json(infra_path)
    if not isinstance(infra, dict):
        raise SkipEmit("estimation-infra.json is not a JSON object")
    projected = infra.get("projected_costs")
    aws_monthly = projected.get("aws_monthly_balanced") if isinstance(projected, dict) else None
    if not _is_amount(aws_monthly):
        raise SkipEmit("no usable projected_costs.aws_monthly_balanced")

    plan: dict = {
        "schemaVersion": SCHEMA_VERSION,
        "sourcePlatform": source_platform,
        "scope": scope,
        "runId": run_id,
        "cost": {
            "awsMonthly": aws_monthly,
            "awsMonthlyBasis": AWS_MONTHLY_BASIS,
        },
    }

    source_monthly = _source_monthly(infra.get("current_costs"), source_platform)
    if source_monthly is not None:
        plan["cost"]["sourceMonthly"] = source_monthly

    service_items = _service_items(projected)
    if service_items:
        plan["cost"]["awsServiceItems"] = service_items

    # The web contract's field is `producerVersion` (named for the producer, not the
    # plugin, so a partner submission needs no second contract) — NOT `pluginVersion`.
    # The schema is strict, so a wrong key would make the whole import fail.
    # producerVersion is optional: a missing OR unreadable plugin.json omits it rather
    # than sinking an otherwise-valid handoff.
    if plugin_json_path.is_file():
        try:
            version = _load_json(plugin_json_path).get("version")
        except Exception:
            # producerVersion is optional, so ANY fault reading plugin.json (decode
            # error, non-object manifest, even a pathological RecursionError) just omits
            # the version — it must never sink an otherwise-valid handoff.
            version = None
        if isinstance(version, str) and version:
            plan["producerVersion"] = version

    return plan, source_platform, scope


def _unlink_quietly(path: Path) -> None:
    """Delete a file if it exists, ignoring any error (best-effort). Used both to drop
    a stale plan.json from an earlier run and to clean up the write temp file."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--migration-dir",
        type=Path,
        required=True,
        help="The run's $MIGRATION_DIR (holds .phase-status.json and estimation-infra.json).",
    )
    parser.add_argument(
        "--plugin-json",
        type=Path,
        default=PLUGIN_ROOT / ".claude-plugin" / "plugin.json",
        help="Path to the plugin manifest read for producerVersion (defaults to this plugin's).",
    )
    args = parser.parse_args()
    migration_dir: Path = args.migration_dir
    out_path = migration_dir / "plan.json"

    try:
        plan, platform, scope = build_plan(migration_dir, args.plugin_json)
    except SkipEmit as skip:
        _unlink_quietly(out_path)
        print(f"PLAN_SKIP | reason={skip}")
        return 0
    except Exception as err:
        # Fail open on ANY error, not just the expected OSError/ValueError: a missed
        # handoff must never break the migration, so an unexpected fault (a corrupt
        # artifact that raises RecursionError/MemoryError, an unforeseen edge) skips
        # cleanly rather than crashing the run with a traceback. SkipEmit is handled
        # above with its specific reason.
        _unlink_quietly(out_path)
        print(f"PLAN_SKIP | reason=unreadable input: {err}")
        return 0

    # Write atomically: render to a sibling temp file, then rename into place. A
    # crash/kill mid-write can then only leave the temp file, never a truncated
    # plan.json the import might ingest.
    tmp_path = out_path.parent / (out_path.name + ".tmp")
    try:
        tmp_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp_path, out_path)
    except Exception as err:
        # Fail open on any write-path fault. os.replace is atomic, so on failure a
        # prior run's plan.json is untouched and still valid — leave it and clean up
        # only our temp, rather than destroying a good handoff.
        _unlink_quietly(tmp_path)
        print(f"PLAN_SKIP | reason=could not write plan.json: {err}")
        return 0

    print(f"PLAN_OK | path={out_path} | platform={platform} | scope={scope}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
