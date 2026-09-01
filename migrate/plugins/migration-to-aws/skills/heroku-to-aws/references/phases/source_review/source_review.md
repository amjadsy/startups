---
_phase: source_review
_title: "Review Application Source (Read-Only)"
_requires_phase: discover
_input:
  - heroku-resource-inventory.json
_fragments:
  - _id: review
    _trigger: { _always: true }
    _file: phases/source_review/source-review-scan.md
_assemble:
  _file: phases/source_review/source-review-assemble.md
_produces:
  - application-source-review.json
_advances_to: clarify
_re_entry_guard:
  _stale_if_completed: clarify
  _stale_artifact: preferences.json
  _on_reentry: stop_unless_confirmed
  _on_confirm: reset_downstream_to_pending
_preconditions:
  - _check_phase_completed: discover
    _on_failure: _halt_and_inform
  - _check_single_active_phase: true
    _on_failure: _halt_and_inform
  - _check_file_exists: heroku-resource-inventory.json
    _on_failure: _unrecoverable
  - _validate_json: heroku-resource-inventory.json
    _on_failure: _unrecoverable
_postconditions:
  - _check_file_exists: application-source-review.json
    _on_failure: _halt_and_inform
  - _validate_json: application-source-review.json
    _on_failure: _halt_and_inform
  - _assert: "application-source-review.json has exactly one reviews[] entry per selected application in inventory order; each entry contains only source_root, request, status, findings, and limitations; status is RETAINED only for a validated complete candidate and UNKNOWN otherwise; and each findings document has exactly one finding per requested question with no duplicate or unrequested question"
    _on_failure: _halt_and_inform
  - _assert: "every findings document validates against shared/application-source-contract.schema.json (#/definitions/findings); a document was retained only when the complete submission passed the tools/application-source-review.ts validator, and any application whose submission was invalid, interrupted, unavailable, unsupported, or over budget was replaced wholesale with one deterministic UNKNOWN finding — each carrying a limitation — per requested question, with no raw rejected output kept"
    _on_failure: _halt_and_inform
  - _assert: "no config var VALUES, literal credentials, or target/architecture/sizing/cost content appear anywhere in application-source-review.json — configuration NAMES only"
    _on_failure: _halt_and_inform
  - _assert: "application-source-review.json is the phase's only output; no source finding is consumed by target selection, Design, Estimate, Generate, reports, or costs in this phase"
    _on_failure: _halt_and_inform
_forbids_files:
  - README.md
  - "*.txt"
  - preferences.json
  - aws-design.json
  - "terraform/**"
---

# Phase: Review Application Source (Read-Only)

## Orientation

Between Discover and Clarify, review each selected application's SOURCE without
executing or modifying it, and record a compact, schema-valid findings document per
application in `application-source-review.json` (`$MIGRATION_DIR/`). The phase is
composed of one FRAGMENT (`source-review-scan.md`, the per-application review) and one
ASSEMBLER (`source-review-assemble.md`, which validates the complete submission and
writes the canonical artifact). Read each unit for its own contract; this phase owns
lifecycle plus the cross-cutting `_postconditions`.

**Fail closed.** A submission is retained WHOLE or replaced WHOLE. Any application
whose complete submission is invalid, interrupted, unavailable, unsupported, or over
budget becomes one deterministic `UNKNOWN` finding per requested question (each with a
limitation). There is no partial acceptance, retry, or resume here — that is PR4.

**Findings are inert in this PR.** Nothing downstream (target selection, Design,
Estimate, Generate, reports, costs) reads `application-source-review.json`. Producing
it must not change Beanstalk, Fargate, or generic EKS behavior.

## Controller ownership

The MAIN controller owns request construction, source-root selection, runtime
validation, canonical artifact writing, and phase state. Do NOT dispatch this phase to
the generic `rw` phase worker — runtime validation must stay controller-owned, so this
phase declares no `_exec`. The only work delegated to a sub-agent is the read-only
review itself (see `source-review-scan.md`), using the dedicated
`source-review-reader` agent when the host supports agents, or the same instructions
inline otherwise.

## Source selection

1. Read `apps[]` from `heroku-resource-inventory.json`; each app with source is
   reviewed once.
2. If exactly one discovered app clearly maps to the current repository, use root `.`.
3. If multiple apps or roots exist, ask ONCE for app-to-root mappings.
4. Roots MUST be workspace-relative and MUST remain inside the workspace after
   realpath/symlink resolution (enforced by `tools/application-source-review.ts`).
5. An app with no source produces requested `UNKNOWN` findings and does not block the
   existing migration flow.

## Question selection

The controller selects the requested questions from accepted Heroku inventory using
`selectQuestions` in `tools/application-source-review.ts` — the 15 always-requested
questions plus conditionals: `health_routes`/`webhooks` for an inbound web process;
`potential_private_endpoints`/`application_connections` for a Private Space or a
selected multi-app estate; `postgresql_extensions` when PostgreSQL is attached;
`redis_usage` when Redis is attached; `addon_usage` for ambiguous attached add-ons. The
reviewer cannot add or remove questions.

## Budgets (per application)

Wall-clock target 300s; 1 concurrent group; 20 model turns; 30 source-tool calls;
100,000 input / 24,000 output tokens; 5,000 source files; 64 MiB total; 2 MiB per file;
256 KiB retained result. File, path, and output limits are enforced locally by
`tools/application-source-review.ts`. Time, tool, turn, and token limits are passed to
the host only where it exposes the control — do not claim hard enforcement otherwise.

## Handoff

After `HANDOFF_OK | phase=source_review`, tell the user how many applications were
reviewed and how many became all-`UNKNOWN` (fail-closed), then: "Next required step:
Phase — Clarify. Load `references/phases/clarify/clarify.md` now."

## Scope Boundary

**This phase reviews application source ONLY.**

FORBIDDEN — Do NOT include ANY of: AWS service names, target/architecture decisions,
sizing, cost, support, or migration recommendations; config var VALUES or literal
credentials; execution or modification of source. **Your ONLY job: record validated
source facts, or `UNKNOWN`. Nothing else.**
