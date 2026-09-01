---
_assemble: assemble-source-review
_of_phase: source_review
_reads:
  - review (fragment contribution)
_produces:
  - application-source-review.json
---

# Source Review — Validate and Assemble

> **Assembler unit.** Runs after the review fragment produced a candidate submission
> per application. It mechanically validates each COMPLETE submission and writes the
> canonical `application-source-review.json`. It owns that artifact's contract and is
> the single creator. Validation is controller-owned and never delegated.

The one authority for validation is `tools/application-source-review.ts` — the same
module the test suite imports, so the checks proved there are the checks applied here.
Apply it per application; there is no partial acceptance.

## Step 1: Validate each complete submission

For each reviewed application, save the request and candidate submission sequentially
as the fixed transient files `$MIGRATION_DIR/.source-review-request.json` and
`$MIGRATION_DIR/.source-review-candidate.json`, then invoke the same validator the
tests import. Never interpolate an application- or user-provided path into the command:

```bash
node "$PLUGIN_ROOT/tools/application-source-review.ts" validate \
  "$PLUGIN_ROOT/skills/heroku-to-aws/references/shared/application-source-contract.schema.json" \
  "$MIGRATION_DIR/.source-review-request.json" \
  "$MIGRATION_DIR/.source-review-candidate.json" "<workspace-root>" \
  "$MIGRATION_DIR/.source-review-roots.json"
```

Create the candidate file with owner-only permissions and invoke validation
immediately. The validator consumes and deletes that file before evaluating it. Delete
the request file after reading the command output; never retain rejected reviewer
output. Handle the command exit code exactly:

|  Exit | Meaning                                    | Action                                                        |
| ----: | ------------------------------------------ | ------------------------------------------------------------- |
|   `0` | Complete submission passed                 | Retain the output's `findings`                                |
|   `1` | Validation ran and rejected the submission | Use the output's deterministic all-`UNKNOWN` `findings`       |
| other | Validator did not run                      | Stop; do not let the controller replace executable validation |

The validator applies `evaluateSubmission({ schema, request, submission, roots,
workspaceRoot })`. It fails the submission when ANY of these hold:

- The request or the findings document does not match the schema.
- The `runtime_framework` finding does not establish Ruby, Java, or Node.js.
- A requested question is missing, duplicated, or an unrequested question appears.
- A shared record reference (`component_id`, `process_id`, `listener_id`,
  `dependency_id`, `callee_application_id`, `inventory_addon_id`, `reference_id`) does
  not resolve.
- An `UNKNOWN` has no limitation, or an absence is qualified by a source-scope
  limitation, or source line bounds are reversed.
- No source root is available; a root is empty, unreadable, or escapes the workspace
  after realpath/symlink resolution; a cited path does not resolve to an existing
  contained file; or a cited line range exceeds the file.
- A source root exceeds 5,000 files, 64 MiB total, or 2 MiB per file; or the retained
  document exceeds 256 KiB.
- Any undeclared value-bearing field, high-confidence literal credential, or
  target/architecture/sizing/cost content is present. Configuration NAMES such as
  `SIGNING_SECRET` remain allowed.

## Step 2: Retain or fail closed

- If `evaluateSubmission` returns `retained: true`, keep its `findings` verbatim.
- Otherwise — invalid, interrupted, unavailable, unsupported, or over budget — replace
  the WHOLE submission with the validator output. For missing source or when no
  candidate submission exists, invoke `application-source-review.ts unknown` with the
  validated request and one fixed reason: `missing_source`, `review_interrupted`,
  `review_unavailable`, or `review_over_budget`. Do NOT let the controller author
  fallback findings. Do NOT keep raw rejected output or salvage individual findings
  from a partially invalid submission (that is PR4).

## Step 3: Write the canonical artifact

Build the transient `$MIGRATION_DIR/.application-source-review.candidate.json` in
inventory order using the exact entry keys shown below. This example is abbreviated;
the actual request contains every inventory-selected question and the findings contain
one canonical `UNKNOWN` per requested question:

```json
{
  "reviews": [
    {
      "source_root": null,
      "request": {
        "application": { "app_id": "example-app", "app_name": "example-app" },
        "requested_questions": ["runtime_framework"],
        "context": {
          "process_types": [],
          "configuration_names": [],
          "postgres_attachment_present": false,
          "redis_attachment_present": false,
          "addon_ids": [],
          "private_space_present": false,
          "selected_estate_application_ids": []
        }
      },
      "status": "UNKNOWN",
      "findings": {
        "findings": [{
          "question": "runtime_framework",
          "status": "UNKNOWN",
          "value": null,
          "sources": [],
          "limitations": [{
            "kind": "OTHER",
            "detail": "Application source was not available for review."
          }]
        }]
      },
      "limitations": ["Application source was not available for review."]
    }
  ]
}
```

`source_root` is the workspace-relative mapping, or `null` when no source was
available. `request` is the validated request. `status` is `RETAINED` only when the
complete candidate passed validation; otherwise it is `UNKNOWN`. `findings` is the
retained or deterministic all-`UNKNOWN` document. `limitations` contains concise
failure reasons and is empty for `RETAINED`. Include one entry per selected
application. Each entry's findings document is at most 256 KiB. Write nothing else —
this artifact is inert for the rest of this PR.

The controller MUST NOT write the canonical artifact. Publish it atomically with the
executable validator:

```bash
node "$PLUGIN_ROOT/tools/application-source-review.ts" publish-artifact \
  "$PLUGIN_ROOT/skills/heroku-to-aws/references/shared/application-source-contract.schema.json" \
  "$MIGRATION_DIR/heroku-resource-inventory.json" \
  "$MIGRATION_DIR/.application-source-review.candidate.json" \
  "<workspace-root>" "$MIGRATION_DIR/application-source-review.json"
```

Exit `0` is the only successful assembly. Exit `1` means the wrapper or one of its
entries failed validation; any other exit means the publisher did not run. In either
case, no stale canonical artifact remains; stop with the reported reasons. The
publisher consumes the candidate file, verifies that its application identities and
order exactly match the inventory, and uses exclusive owner-only temporary-file
creation before atomic rename. Delete all remaining transient request, root, and
selection files after successful publication or failure.

**If assembly cannot produce a valid document for an application** even as the
deterministic `UNKNOWN` replacement, that is an unrecoverable error
(`INTERPRETER.md` § `_on_error` — `_unrecoverable`): STOP and report which application
failed. The phase `_postconditions` independently re-check schema validity, the
one-finding-per-question rule, and the no-values/no-target constraints.
