#!/usr/bin/env python3
"""Resolve a plan-stated source model ID against the live provider catalog.

Usage:
  PLAN_MODEL_ID=<id> python resolve_source_model.py <path/to/.source-provider-env>

Prints one JSON object on stdout:
  {"status": "exact"|"prefix", "resolved_id": ..., "all_hits": [...]}
  {"status": "not_found", "candidates": [...], "ambiguous_prefix": true?}
  {"status": "no_key"}   (exit 2)

Hard rule — model line is sacred: auto-resolution may only pin the SAME model
line to a date (YYYY-MM-DD) or pure version suffix. Any alphabetic suffix
(mini, pro, turbo, latest, codex, ...) is a different line or a moving alias
and is surfaced to the user instead of picked. See
references/helpers/run-source-model-baseline/run-source-model-baseline.md.

Security: the key is read from the env file and sent ONLY as an auth header to
the provider's own official endpoint (api.openai.com / api.anthropic.com /
generativelanguage.googleapis.com) — never to any other host and never as a
URL query parameter. The key value is never printed.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request

SAFE_SUFFIX = re.compile(r"^\d{4}-\d{2}-\d{2}$|^\d[\d.]*$")


def load_env_file(path: str) -> None:
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k] = v


def list_openai() -> list[str]:
    req = urllib.request.Request(
        "https://api.openai.com/v1/models",
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:  # nosec B310 — fixed https URL
        return [m["id"] for m in json.loads(r.read())["data"]]


def list_anthropic() -> list[str]:
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/models",
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:  # nosec B310 — fixed https URL
        return [m["id"] for m in json.loads(r.read())["data"]]


def list_gemini() -> list[str]:
    # Key travels as a header, not a query parameter — URLs end up in logs.
    req = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/models",
        headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
    )
    with urllib.request.urlopen(req, timeout=30) as r:  # nosec B310 — fixed https URL
        # Gemini returns names like "models/gemini-1.5-pro"; strip the prefix.
        return [m["name"].split("/", 1)[-1] for m in json.loads(r.read()).get("models", [])]


def safe_variant(catalog_id: str, plan_id: str) -> bool:
    if catalog_id == plan_id:
        return True
    suffix = catalog_id[len(plan_id) + 1:]  # strip "PLAN_ID-"
    return bool(SAFE_SUFFIX.match(suffix))


def _lcp(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def resolve(catalog: list[str], plan_id: str) -> dict:
    """Pure resolution decision — no network, unit-tested."""
    if plan_id in catalog:
        return {"status": "exact", "resolved_id": plan_id}

    prefix_hits = [m for m in catalog if m == plan_id or m.startswith(plan_id + "-")]

    # A bare prefix match is NOT enough to auto-resolve: "gpt-4o-mini",
    # "claude-3-5-sonnet-latest" start with a plausible plan ID but are
    # different model lines / non-deterministic aliases. Only a date or pure
    # version suffix is the same line.
    safe_hits = sorted((m for m in prefix_hits if safe_variant(m, plan_id)), key=len)
    if safe_hits:
        return {"status": "prefix", "resolved_id": safe_hits[0], "all_hits": safe_hits}

    if prefix_hits:
        return {"status": "not_found", "candidates": prefix_hits[:5], "ambiguous_prefix": True}

    ranked = sorted(catalog, key=lambda m: -_lcp(m, plan_id))[:5]
    return {"status": "not_found", "candidates": ranked}


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: PLAN_MODEL_ID=<id> resolve_source_model.py <env-file>", file=sys.stderr)
        return 2
    load_env_file(sys.argv[1])
    plan_id = os.environ["PLAN_MODEL_ID"]

    if "OPENAI_API_KEY" in os.environ:
        catalog = list_openai()
    elif "ANTHROPIC_API_KEY" in os.environ:
        catalog = list_anthropic()
    elif "GEMINI_API_KEY" in os.environ:
        catalog = list_gemini()
    else:
        print(json.dumps({"status": "no_key"}))
        return 2

    print(json.dumps(resolve(catalog, plan_id)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
