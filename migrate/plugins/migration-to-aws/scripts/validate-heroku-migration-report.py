#!/usr/bin/env python3
"""Validate heroku-to-aws migration report HTML (thin stakeholder report).

Two modes, sharing the decision-core sections (see
skills/heroku-to-aws/references/shared/report-decision-core.md):

  full     (default) migration-report.html — decision-summary, exec-costs,
           next-steps required; decision-basis / what-if-scenarios conditional.
  decision decision-report.html — decision-summary, exec-costs required;
           decision-cta required instead of next-steps; decision-basis /
           what-if-scenarios conditional (same triggers as full mode).

Exit 0 on PASS, 1 on FAIL.

Usage:
  python3 validate-heroku-migration-report.py /path/to/migration-report.html \\
      --migration-dir "$MIGRATION_DIR"
  python3 validate-heroku-migration-report.py /path/to/decision-report.html \\
      --mode decision
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

# Required in both modes.
COMMON_REQUIRED_SECTION_IDS = [
    "decision-summary",
    "exec-costs",
]

# The one structural difference between modes: decision mode ends on a CTA
# pointing at Generate instead of the full report's next-steps list (which
# assumes MIGRATION_GUIDE.md / terraform/ already exist — they don't yet in
# decision mode).
MODE_REQUIRED_SECTION_ID = {
    "full": "next-steps",
    "decision": "decision-cta",
}


class _SectionOpenTagCollector(HTMLParser):
    """Collect the `id` of every real (rendered) <section> open tag.

    Uses the stdlib parser rather than a regex so that a <section id="..."> that
    only exists inside an HTML comment (e.g. an unexpanded template placeholder
    like `<!-- <section id="decision-basis"> when ... -->`) is never counted as
    present — HTMLParser routes comment text to handle_comment, never
    re-tokenizing it as a real tag, whereas a regex scanning raw source text
    cannot distinguish a real tag from one that merely looks like a tag inside
    a comment."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.section_ids: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "section":
            sid = dict(attrs).get("id")
            if sid:
                self.section_ids.append(sid)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)


def _section_counts(html: str) -> dict[str, int]:
    parser = _SectionOpenTagCollector()
    parser.feed(html)
    parser.close()
    counts: dict[str, int] = {}
    for sid in parser.section_ids:
        counts[sid] = counts.get(sid, 0) + 1
    return counts


class _SectionScopeParser(HTMLParser):
    """Locate <section id="..."> ... </section> by parsed tag structure rather
    than a literal `<section ...>(.*?)</section>` regex, so any legal closing-
    tag spelling — `</section>`, `</section >` (trailing whitespace), or a
    newline before the `>` — is still recognized. A regex anchored to the
    exact literal string `</section>` misses these; a real HTML parser's
    handle_endtag fires the same regardless of how the tag was serialized."""

    def __init__(self, target_id: str) -> None:
        super().__init__(convert_charrefs=True)
        self.target_id = target_id
        self.found_html: str | None = None
        self._depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "section":
            if self._depth > 0:
                self._parts.append(self.get_starttag_text() or "")
            return
        if self._depth > 0:
            self._depth += 1
            self._parts.append(self.get_starttag_text() or "")
            return
        if dict(attrs).get("id") == self.target_id:
            self._depth = 1
            self._parts = []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._depth > 0:
            self._parts.append(self.get_starttag_text() or "")

    def handle_endtag(self, tag: str) -> None:
        if tag != "section" or self._depth == 0:
            if self._depth > 0:
                self._parts.append(f"</{tag}>")
            return
        self._depth -= 1
        if self._depth == 0:
            if self.found_html is None:
                self.found_html = "".join(self._parts)
        else:
            self._parts.append("</section>")

    def handle_data(self, data: str) -> None:
        if self._depth > 0:
            # Re-escape markup-syntax characters before appending. With
            # convert_charrefs=True, handle_data receives ALREADY-DECODED
            # text: an escaped code example like `&lt;span
            # data-cost-key="x"&gt;$112&lt;/span&gt;` decodes to the literal
            # text `<span data-cost-key="x">$112</span>` — indistinguishable,
            # once appended into the returned string, from a REAL <span> tag
            # that was never actually in the source. Re-escaping `<`, `>`,
            # and `&` (the only characters that make reparsed text look like
            # markup) makes the returned string round-trip safely through a
            # second HTMLParser pass (e.g. _cost_anchor_matches) while
            # leaving every other decoded character as plain text.
            self._parts.append(
                data.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            )

    def handle_comment(self, data: str) -> None:
        if self._depth > 0:
            self._parts.append(f"<!--{data}-->")


def _section_html(html: str, section_id: str) -> str | None:
    """Return the inner HTML of the first <section id="section_id"> in `html`,
    found via parsed tag structure (any legal attribute/closing-tag spelling),
    or None when that section is genuinely absent. Non-nesting semantics
    preserved (first section with a matching id wins) — the Heroku report
    skeleton never nests <section> elements."""
    parser = _SectionScopeParser(section_id)
    parser.feed(html)
    parser.close()
    return parser.found_html


def _normalize_money(text: str) -> str | None:
    """Reduce a rendered money string to its canonical display form for exact
    comparison against the JSON figure (same normalization on both sides via
    `_canonical_money`). '$1,415/mo' -> '1415'; '$112.90' -> '113'; '$0.40' ->
    '0.40'. Returns None when no dollar amount is present. Cents are NO LONGER
    truncated — a correctly rounded report figure must match, not be rejected."""
    m = re.search(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)", text)
    if not m:
        return None
    try:
        return _canonical_money(float(m.group(1).replace(",", "")))
    except (TypeError, ValueError):
        return None


def _canonical_money(value: float) -> str:
    """Canonical display form for a dollar amount, matching the emitter's own
    rule (generate-report.md currency rule / the currency-formatting gate):
    monthly-scale totals (>= $2 after rounding) round to the nearest whole
    dollar; genuinely small totals keep two-decimal cents. Both the rendered
    figure and the JSON figure pass through this SAME function before comparison,
    so a correctly rounded `$113` for `112.90` matches (not truncated to `112`),
    and the small-total exception (e.g. `$0.40`) is preserved instead of being
    truncated to `0`. Decides precision on the ROUNDED magnitude so a value that
    rounds up across the $2 threshold (e.g. 1.999) canonicalizes to `"2"`,
    matching a displayed `$2`, instead of `"2.00"`."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise
    rounded_whole = int(round(v))
    if abs(rounded_whole) >= _CENTS_MEANINGFUL_BELOW:
        return str(rounded_whole)  # nearest-dollar; 112.90->113, 112.4->112, 1.999->2
    return f"{v:.2f}"  # genuinely small total: retain cents (0.40 -> "0.40")


# Elements whose subtree the browser never renders — an anchor (or its text)
# inside one must never stand in for the visible figure. Mirrors the currency
# text parser's inert set so the anchor collector and the currency parser agree
# on what "rendered" means.
_ANCHOR_INERT_TAGS = {"script", "style", "template"}


# data-cost-key anchor -> estimation-infra.json path. Heroku asserts the recommended
# AWS monthly (Balanced) figure only; the current-spend comparator is a follow-up
# (Heroku's current_costs key is not yet settled — heroku_monthly_baseline vs _estimated).
_COST_ANCHORS = {
    "aws_monthly_balanced": ("projected_costs", "aws_monthly_balanced"),
}
# Load-bearing key: when its JSON value exists AND exec-costs is present, the anchor
# MUST be present (a missing anchor is a FAIL, not a skip — otherwise an un-anchored
# wrong figure passes, the bug P1-C exists to catch).
_REQUIRED_COST_KEYS = ("aws_monthly_balanced",)


class _CostAnchorParser(HTMLParser):
    """Collect the rendered text of every `data-cost-key="..."` element.

    Ported from validate-migration-report.py's parser (GCP) so both providers
    agree on what "rendered" means. Uses the stdlib HTML parser rather than a
    regex so that:
    (a) markup inside an HTML comment is never mistaken for a real anchor —
        comments are a distinct token the parser never re-tokenizes as tags;
    (b) nested child markup is read through the anchored element's OWN matching
        close tag, not the first `</` encountered, by counting nested
        opens/closes — and a NESTED `data-cost-key` element is collected as its
        own anchor too (an outer anchor being open must not swallow a recognized
        inner figure), tracked on a stack;
    (c) inert subtrees (`<script>`, `<style>`, `<template>`) are skipped — their
        content is never rendered by the browser, so an anchor or dollar token
        placed there must not satisfy the visible-figure requirement, even when
        the inert element itself carries `data-cost-key`;
    (d) an element with a `hidden` attribute is skipped for the same reason —
        its subtree is not rendered.
    Character references are decoded automatically (`convert_charrefs=True`).
    """

    # Void elements never have an end tag, so they must not be pushed onto the
    # element stack (doing so would desync every subsequent close).
    _VOID_TAGS = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[tuple[str, str]] = []  # (key, inner text), document order
        # One frame per open non-void element, innermost last. Each frame:
        #   {"tag", "inert": bool, "hidden": bool, "anchor": {key,parts}|None}
        # `inert`/`hidden` are STICKY down the subtree (an element inside an inert
        # or hidden ancestor is itself skipped) — computed as ancestor-or-self.
        self._stack: list[dict] = []

    def _in_skip(self) -> bool:
        """True when the current point is inside an inert or hidden subtree."""
        return bool(self._stack) and (self._stack[-1]["inert"] or self._stack[-1]["hidden"])

    @staticmethod
    def _is_hidden(attrs: list[tuple[str, str | None]]) -> bool:
        # `hidden` is a BOOLEAN attribute: its mere presence hides the subtree,
        # regardless of value. In HTML `hidden="false"` is NOT a not-hidden value —
        # "false" is an invalid value for a boolean attribute, whose invalid-value
        # default is the Hidden state. So any `hidden` attribute (including
        # `hidden=""`, `hidden="hidden"`, and `hidden="false"`) hides the element;
        # only the attribute's ABSENCE leaves it visible.
        return "hidden" in dict(attrs)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        parent = self._stack[-1] if self._stack else None
        # inert/hidden are STICKY: an element inside an inert/hidden ancestor is
        # itself inert/hidden. Kept as clean booleans (never a truthy list).
        inert = bool(parent and parent["inert"]) or tag in _ANCHOR_INERT_TAGS
        hidden = bool(parent and parent["hidden"]) or self._is_hidden(attrs)
        anchor = None
        key = dict(attrs).get("data-cost-key")
        # Start a new anchor only when this element is actually rendered.
        if key and not inert and not hidden:
            anchor = {"key": key.lower(), "parts": []}
        frame = {"tag": tag, "inert": inert, "hidden": hidden, "anchor": anchor}
        if tag not in self._VOID_TAGS:
            self._stack.append(frame)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _ANCHOR_INERT_TAGS or self._in_skip():
            return
        parent_hidden = self._stack[-1]["hidden"] if self._stack else False
        key = dict(attrs).get("data-cost-key")
        # A self-closed anchor has no text content; record it (empty) only if rendered.
        if key and not parent_hidden and not self._is_hidden(attrs):
            self.results.append((key.lower(), ""))

    def handle_endtag(self, tag: str) -> None:
        if tag in self._VOID_TAGS:
            return
        # Pop to the nearest matching open tag (tolerate minor misnesting). Every
        # frame in the popped slice that carried an anchor is emitted — including
        # any INNER anchors implicitly closed by an outer element's end tag — so a
        # nested `data-cost-key` is never silently dropped. Emit innermost-first,
        # then the matched frame, all in the order they closed.
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i]["tag"] == tag:
                popped = self._stack[i:]
                del self._stack[i:]
                for frame in reversed(popped):  # innermost closes first
                    if frame["anchor"] is not None:
                        self.results.append(
                            (frame["anchor"]["key"], "".join(frame["anchor"]["parts"]))
                        )
                return
        # Unmatched close tag: ignore.

    def handle_data(self, data: str) -> None:
        if self._in_skip():
            return
        # Append rendered text to every open anchor on the stack (an outer
        # anchor's text legitimately includes its children's text).
        for frame in self._stack:
            if frame["anchor"] is not None:
                frame["anchor"]["parts"].append(data)


def _cost_anchor_matches(html: str) -> list[tuple[str, str]]:
    """Parse `html` and return every (data-cost-key, rendered text) pair found
    outside comments and non-rendered markup (script/style/template/hidden),
    including nested recognized anchors."""
    parser = _CostAnchorParser()
    parser.feed(html)
    parser.close()
    return parser.results


def _dig(d: dict, path: tuple[str, ...]):
    cur = d
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def _validate_cost_figures(html: str, migration_dir: Path | None) -> list[str]:
    """Assert the report's cost figures match estimation-infra.json (P1-C).

    Fail direction:
    - No estimation-infra.json / corrupt / not a dict -> skip (fail open on absence).
    - aws_monthly_balanced present in JSON + exec-costs present -> an anchor MUST
      exist INSIDE <section id="exec-costs">; a missing anchor there FAILs (an
      un-anchored wrong figure, or an anchor placed elsewhere e.g. decision-summary,
      must not pass) — mirrors validate-migration-report.py's required-anchors
      section scoping.
    - Anchor present anywhere + JSON value present but rendered dollars differ -> FAIL
      (any anchor is still cross-checked against the estimate, even outside exec-costs).
    - Anchored element with a real JSON value but no $ rendered -> FAIL.
    - Non-numeric / non-whole-dollar JSON value -> FAIL (named), never a crash.
    - Unknown anchor key -> skip.
    """
    if migration_dir is None:
        return []
    est_path = migration_dir / "estimation-infra.json"
    if not est_path.is_file():
        return []
    try:
        est = json.loads(est_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []  # fail open on ambiguity: a corrupt estimate does not gate the report
    if not isinstance(est, dict):
        return []
    errors: list[str] = []

    for key, text in _cost_anchor_matches(html):
        key = key.lower()
        path = _COST_ANCHORS.get(key)
        if path is None:
            continue
        expected = _dig(est, path)
        if expected is None:
            continue
        try:
            # Normalize the JSON figure to the SAME display precision the emitter
            # renders at (nearest dollar for monthly-scale, cents for small
            # totals) so a correctly rounded report matches — not a truncation.
            expected_dollars = _canonical_money(float(expected))
        except (TypeError, ValueError):
            errors.append(
                f'estimation-infra.json {".".join(path)} is not a numeric dollar '
                f"amount: {expected!r}"
            )
            continue
        rendered = _normalize_money(text)
        if rendered is None:
            errors.append(
                f'data-cost-key="{key}" element renders no dollar amount '
                f"(expected ${expected_dollars} from {'.'.join(path)})"
            )
            continue
        if expected_dollars != rendered:
            errors.append(
                f'cost figure mismatch: data-cost-key="{key}" renders "${rendered}" '
                f'but estimation-infra.json {".".join(path)} = ${expected_dollars}'
            )

    # Required figures must be anchored INSIDE <section id="exec-costs"> when their
    # JSON value exists and that section is rendered. A redundant anchor elsewhere
    # (e.g. a decision-summary hero metric) is still cross-checked by the mismatch
    # loop above, but does not satisfy this requirement: exec-costs is the section
    # customers read as the authoritative cost comparison.
    exec_costs_html = _section_html(html, "exec-costs")
    if exec_costs_html is not None:
        exec_costs_keys = {k.lower() for k, _ in _cost_anchor_matches(exec_costs_html)}
        for key in _REQUIRED_COST_KEYS:
            path = _COST_ANCHORS[key]
            if _dig(est, path) is None:
                continue
            if key not in exec_costs_keys:
                errors.append(
                    f'missing data-cost-key="{key}" anchor inside '
                    f'<section id="exec-costs">; cannot confirm the rendered figure '
                    f"matches estimation-infra.json {'.'.join(path)} (wrap that "
                    f'figure in <span data-cost-key="{key}">...</span> inside '
                    f"exec-costs)"
                )

    return errors


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
# full rationale; kept identical here so both validators stay in sync. The
# Heroku report has no documented Calculation/Notes column (its exec-costs
# section is a Heroku-vs-AWS side-by-side or three-tier table, per
# generate-report.md — no per-service arithmetic show-work column), so this
# copy has no calc-column exemption; everything else ports unchanged.
CENTS_RE = re.compile(r"\$([0-9][0-9,]*)\.([0-9]{2})\b")

# Deliberately does NOT accept a BARE "month"/"mo" as itself the qualifying
# unit — see validate-migration-report.py's _RATE_SUFFIX_RE comment for the
# full rationale (a bare "/mo" is exactly the unit an ordinary monthly total
# is denominated in, not evidence of a per-unit rate). "/mo per <unit>" is
# still accepted (e.g. "$5.00/mo per policy").
_RATE_SUFFIX_RE = re.compile(
    r"^\s*(?:/|\(|\bper\b)?\s*(?:mo\b\s*(?:per\b\s*)?)?"
    r"(?:hr|hour|hourly|vcpu|gb|gib|tb|image|unit|policy|1m|10k|"
    r"[0-9]+-mo)\b",
    re.IGNORECASE,
)

_CENTS_MEANINGFUL_BELOW = 2


class _DecodedTextParser(HTMLParser):
    """Extract rendered text as the browser would present it — entities
    decoded, comments and inert content (script/style/template) excluded —
    while preserving amount/unit adjacency across inline markup (mirrors
    validate-migration-report.py's _DecodedTextRunParser; see that file's
    class docstring for the full rationale). No Calculation/Notes column
    tracking here — the Heroku report has no such column."""

    _INLINE_TAGS = {
        "a", "abbr", "b", "bdi", "bdo", "cite", "code", "data", "dfn", "em",
        "i", "kbd", "mark", "q", "s", "samp", "small", "span", "strong",
        "sub", "sup", "time", "u", "var", "wbr",
    }
    _INERT_TAGS = {"script", "style", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._inert_depth = 0
        # Absolute offsets (into text()) of every block-level separator — a
        # rate-suffix match must never read past one of these into unrelated
        # content from a different cell/row/paragraph (see
        # validate-migration-report.py's identical tracking for the full
        # rationale — a single separating space does not itself stop a
        # word-based regex when the next block happens to start with a real
        # rate-unit word like "Hourly").
        self._boundaries: list[int] = []

    def _append(self, text: str, *, is_boundary: bool = False) -> None:
        if not text:
            return
        if is_boundary:
            self._boundaries.append(sum(len(p) for p in self._parts))
        self._parts.append(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._INERT_TAGS:
            self._inert_depth += 1
            return
        if self._inert_depth == 0 and tag not in self._INLINE_TAGS:
            self._append(" ", is_boundary=True)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in self._INLINE_TAGS and tag not in self._INERT_TAGS:
            self._append(" ", is_boundary=True)

    def handle_endtag(self, tag: str) -> None:
        if tag in self._INERT_TAGS:
            if self._inert_depth > 0:
                self._inert_depth -= 1
            return
        if self._inert_depth == 0 and tag not in self._INLINE_TAGS:
            self._append(" ", is_boundary=True)

    def handle_data(self, data: str) -> None:
        if self._inert_depth == 0:
            self._append(data)

    def text(self) -> str:
        return "".join(self._parts)

    def boundaries(self) -> list[int]:
        return self._boundaries


def _decoded_text(html: str) -> tuple[str, list[int]]:
    parser = _DecodedTextParser()
    parser.feed(_body_scope(html))
    parser.close()
    return parser.text(), parser.boundaries()


def _validate_currency_formatting(html: str) -> list[str]:
    """Monthly cost figures must render as whole dollars. Flag any $X.YY
    figure whose whole-dollar part is >= $2 and that is not immediately
    followed by a per-unit-rate suffix (/hr, per policy, etc.)."""
    errors: list[str] = []
    text, boundaries = _decoded_text(html)
    seen: set[str] = set()
    for match in CENTS_RE.finditer(text):
        whole = int(match.group(1).replace(",", ""))
        if whole < _CENTS_MEANINGFUL_BELOW:
            continue
        cutoff = match.end() + 25
        for boundary in boundaries:
            if boundary >= match.end():
                cutoff = min(cutoff, boundary)
                break
        trailing = text[match.end():cutoff]
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


def validate(html: str, migration_dir: Path | None, mode: str = "full") -> list[str]:
    errors: list[str] = []
    counts = _section_counts(html)

    required = [*COMMON_REQUIRED_SECTION_IDS, MODE_REQUIRED_SECTION_ID[mode]]
    for sid in required:
        n = counts.get(sid, 0)
        if n == 0:
            errors.append(f'missing required <section id="{sid}">')
        elif n > 1:
            errors.append(f'duplicate <section id="{sid}"> ({n} occurrences)')

    # The other mode's terminal section must NOT appear — decision-report.html
    # must not carry a next-steps pointer into an execution pack that does not
    # exist yet, and migration-report.html should not carry the pre-execution
    # decision-cta once the real thing (next-steps) exists.
    other_mode = "decision" if mode == "full" else "full"
    other_terminal = MODE_REQUIRED_SECTION_ID[other_mode]
    if counts.get(other_terminal, 0) >= 1:
        errors.append(
            f'--mode {mode} report must not contain <section id="{other_terminal}"> '
            f"(that is the {other_mode}-mode terminal section)"
        )

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

        # generate-report.md / report-decision-core.md § decision-basis: when
        # Estimate declared decision_basis (evidence/assumptions behind the
        # verdict), the report MUST render it — in both modes, since decision
        # mode reuses these exact content rules rather than restating them.
        # Read the same estimation-infra.json the report itself was built
        # from, so a report that silently drops decision_basis (e.g. a
        # refactor that forgets the section) cannot still say REPORT_OK.
        est_path = migration_dir / "estimation-infra.json"
        if est_path.is_file():
            try:
                est = json.loads(est_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                est = None
            decision_basis = ((est or {}).get("recommendation") or {}).get("decision_basis")
            if decision_basis and counts.get("decision-basis", 0) < 1:
                errors.append(
                    "estimation-infra.json declares recommendation.decision_basis "
                    'but the report has no <section id="decision-basis"> '
                    '("What This Assessment Rests On")'
                )

    if mode == "decision" and migration_dir is not None:
        # Decision mode's real invariant: THIS decide-complete cycle has not
        # itself gone through Generate yet (phases.generate is "pending" or
        # absent). It is NOT "no terraform/ or generation-*.json file exists
        # on disk" — a prior Generate/workshop-reprice cycle's execution pack
        # can legitimately still be sitting there (workshop re-entry
        # preserves it deliberately: it may hold customer-edited baseline.tf/
        # variables.tf or hand-authored terraform.tfvars/state that cannot be
        # safely deleted). Treating raw file presence as the signal made a
        # perfectly valid decision, after a workshop reprice on a
        # previously-executed run, permanently unable to pass — the pre-
        # execution claim this check exists to make ("no code has been
        # generated for the CURRENT decision") was never really about the
        # filesystem; it's about .phase-status.json's own bookkeeping.
        #
        # phases.generate == "completed"/"in_progress" is exactly the signal
        # that consent to execute for the CURRENT cycle was already given —
        # that state is precisely what "decision mode" (pre-execution) must
        # not be, and .phase-status.json is the interpreter's own source of
        # truth for it (see phase-status.schema.json's run_mode/phases
        # description).
        #
        # Fail open ONLY on a genuinely MISSING status file — that means no
        # run has ever tracked state here, which is not evidence of anything
        # (e.g. the isolated unit-test path validating HTML without a real
        # $MIGRATION_DIR). Do NOT fail open on a file that EXISTS but is
        # unreadable or fails to parse as JSON: that is state corruption, and
        # INTERPRETER.md § State-file validation is explicit that invalid
        # JSON is a STOP condition ("do not proceed or guess"), not something
        # to treat as equivalent to "no state exists." Guessing "pending"
        # past a corrupt file would let a broken run silently pass the one
        # check this mode exists to enforce.
        phase_path = migration_dir / ".phase-status.json"
        generate_status: str | None = None
        if phase_path.is_file():
            try:
                phase_text = phase_path.read_text(encoding="utf-8")
                if not phase_text.strip():
                    raise json.JSONDecodeError("empty file", phase_text, 0)
                phase = json.loads(phase_text)
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(
                    "decision mode: .phase-status.json exists but could not "
                    f"be read/parsed ({exc}) — state corrupted (invalid "
                    "JSON). Delete the file and restart the current phase "
                    "(INTERPRETER.md § State-file validation); an unreadable "
                    "state file is not evidence of a pre-execution decision"
                )
            else:
                generate_status = (phase or {}).get("phases", {}).get("generate")
        if generate_status in ("completed", "in_progress"):
            errors.append(
                "decision mode: .phase-status.json phases.generate is "
                f"{generate_status!r} — this decide-complete cycle already "
                "went through Generate; decision mode is pre-execution only "
                "for the CURRENT cycle (a prior cycle's execution pack may "
                "legitimately remain on disk after a workshop reprice)"
            )
    errors.extend(_validate_cost_figures(html, migration_dir))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report_path", type=Path)
    parser.add_argument("--migration-dir", type=Path, default=None)
    parser.add_argument(
        "--mode",
        choices=["full", "decision"],
        default="full",
        help="full = migration-report.html (default); decision = decision-report.html",
    )
    args = parser.parse_args()

    if not args.report_path.is_file():
        print(f"REPORT_FAIL | file={args.report_path} | reason=not_found", file=sys.stderr)
        return 1

    html = args.report_path.read_text(encoding="utf-8")
    errors = validate(html, args.migration_dir, args.mode)
    if errors:
        print(f"REPORT_FAIL | file={args.report_path} | mode={args.mode} | errors={len(errors)}", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    counts = _section_counts(html)
    optional = []
    if counts.get("what-if-scenarios", 0) >= 1:
        optional.append("what-if-scenarios")
    if counts.get("decision-basis", 0) >= 1:
        optional.append("decision-basis")
    required_count = len(COMMON_REQUIRED_SECTION_IDS) + 1
    print(
        "REPORT_OK | structure=complete | mode="
        f"{args.mode} | sections={required_count}/{required_count}"
        + (f" | optional={','.join(optional)}" if optional else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
