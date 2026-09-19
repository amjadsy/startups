#!/usr/bin/env python3
"""Validate heroku-to-aws migration-report.html (thin stakeholder report).

Required sections: decision-summary, exec-costs, next-steps.
Conditional: what-if-scenarios when scenarios/index.json has ≥2 entries.
Footer must contain "draft for review".

Exit 0 on PASS, 1 on FAIL.

Usage:
  python3 validate-heroku-migration-report.py /path/to/migration-report.html \\
      --migration-dir "$MIGRATION_DIR"
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REQUIRED_SECTION_IDS = [
    "decision-summary",
    "exec-costs",
    "next-steps",
]

SECTION_OPEN = re.compile(
    r'<section\b[^>]*\bid=["\']([^"\']+)["\'][^>]*>',
    re.IGNORECASE,
)


def _section_counts(html: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for match in SECTION_OPEN.finditer(html):
        sid = match.group(1)
        counts[sid] = counts.get(sid, 0) + 1
    return counts


def _body_scope(html: str) -> str:
    """Body only, excluding <style> blocks, so CSS hex/decimal values never
    trip the currency-formatting check (mirrors the GCP validator's
    _readability_scope)."""
    no_style = re.sub(r"<style\b.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    body = re.search(r"<body\b[^>]*>(.*?)</body>", no_style, re.DOTALL | re.IGNORECASE)
    return body.group(1) if body else no_style


# Ported from validate-migration-report.py — same currency-formatting rule
# (monthly figures render as whole dollars; cents are reserved for genuinely
# sub-dollar precision or per-unit rates). See that file's comment for the
# full rationale; kept identical here so both validators stay in sync.
CENTS_RE = re.compile(r"\$([0-9][0-9,]*)\.([0-9]{2})\b")

_RATE_SUFFIX_RE = re.compile(
    r"^\s*(?:/|\(|\bper\b)?\s*"
    r"(?:hr|hour|hourly|vcpu|gb|gib|tb|image|unit|policy|1m|10k|month|"
    r"mo\b\s*per\b|[0-9]+-mo\b)",
    re.IGNORECASE,
)

_CENTS_MEANINGFUL_BELOW = 2


def _validate_currency_formatting(html: str) -> list[str]:
    """Monthly cost figures must render as whole dollars. Flag any $X.YY
    figure whose whole-dollar part is >= $2 and that is not immediately
    followed by a per-unit-rate suffix (/hr, per policy, etc.)."""
    errors: list[str] = []
    scope = _body_scope(html)
    seen: set[str] = set()
    for match in CENTS_RE.finditer(scope):
        whole = int(match.group(1).replace(",", ""))
        if whole < _CENTS_MEANINGFUL_BELOW:
            continue
        trailing = scope[match.end():match.end() + 25]
        if _RATE_SUFFIX_RE.match(trailing):
            continue
        token = match.group(0)
        if token in seen:
            continue
        seen.add(token)
        errors.append(
            f'currency formatting: "{token}" renders cents on a monthly-scale '
            "figure — round to a whole dollar (cents only for genuinely "
            'sub-dollar precision, e.g. "$1.50", "$0.40", or a per-unit rate '
            'like "$0.018/hr")'
        )
    return errors


def validate(html: str, migration_dir: Path | None) -> list[str]:
    errors: list[str] = []
    counts = _section_counts(html)

    for sid in REQUIRED_SECTION_IDS:
        n = counts.get(sid, 0)
        if n == 0:
            errors.append(f'missing required <section id="{sid}">')
        elif n > 1:
            errors.append(f'duplicate <section id="{sid}"> ({n} occurrences)')

    if "draft for review" not in html.lower():
        errors.append('footer must contain "draft for review" disclaimer')

    errors.extend(_validate_currency_formatting(html))

    if migration_dir is not None:
        index_path = migration_dir / "scenarios" / "index.json"
        if index_path.is_file():
            try:
                index = json.loads(index_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                index = None
            scenarios = (index or {}).get("scenarios") or []
            if len(scenarios) >= 2 and counts.get("what-if-scenarios", 0) < 1:
                errors.append(
                    'scenarios/index.json has ≥2 scenarios but no '
                    '<section id="what-if-scenarios">'
                )

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report_path", type=Path)
    parser.add_argument("--migration-dir", type=Path, default=None)
    args = parser.parse_args()

    if not args.report_path.is_file():
        print(f"REPORT_FAIL | file={args.report_path} | reason=not_found", file=sys.stderr)
        return 1

    html = args.report_path.read_text(encoding="utf-8")
    errors = validate(html, args.migration_dir)
    if errors:
        print(f"REPORT_FAIL | file={args.report_path} | errors={len(errors)}", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    counts = _section_counts(html)
    optional = []
    if counts.get("what-if-scenarios", 0) >= 1:
        optional.append("what-if-scenarios")
    print(
        "REPORT_OK | structure=complete | sections="
        f"{len(REQUIRED_SECTION_IDS)}/{len(REQUIRED_SECTION_IDS)}"
        + (f" | optional={','.join(optional)}" if optional else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
