# Discover Phase: OpenAI Usage API Discovery

> Self-contained OpenAI usage discovery sub-file. Captures real cost and
> token-usage data directly from the OpenAI Admin API — read-only, consent-gated,
> aggregate counts only — as an alternative to the user exporting billing CSVs by
> hand. Produces `openai-usage-profile.json` and, when `ai-workload-profile.json`
> exists, fills its `current_costs` section with real spend. If the user declines
> consent or has no Admin API key, exits cleanly with no output.

**Execute ALL steps in order. Do not skip or optimize.**

---

## Security Contract (applies to every step)

1. **Exact-endpoint whitelist, GET only.** Call ONLY the endpoints in the Step 2
   Capture Endpoint Table. All are `GET` against `https://api.openai.com`. Never
   any other endpoint, never any other HTTP method, never endpoints that return
   member/user lists, API keys, or request/response content.
2. **The Admin key must never enter this conversation (HARD RULE).** Do not ask
   the user to paste the key in chat, and never echo, cat, or interpolate its
   VALUE into any command, question, or output — the agent only ever handles the
   file path `$MIGRATION_DIR/.openai-admin-env` (`chmod 600`, inside the
   gitignored `.migration/` tree). If the user pastes a key into the chat
   unprompted, do not use it: tell them it is now part of the transcript,
   recommend rotating it, and continue with the Step 0 intake paths. All API
   calls go through a throwaway capture script that reads the key from the
   file. Only a sha256 fingerprint of the file appears in the manifest.
3. **Aggregate data only.** The usage endpoints return bucketed token counts and
   cost amounts — no prompts, no completions, no file contents. Do not add
   `group_by=user_id` or `group_by=api_key_id` to any call: model-level
   granularity is all downstream phases need.
4. **Capture to files, not context.** The capture script writes responses under
   `$MIGRATION_DIR/openai-capture/`. Parse capture files with a throwaway
   extraction script if any exceeds ~500 buckets — do NOT Read oversized raw
   captures into context.
5. **Consent first.** No API call runs before the user answers `[A]` in Step 1.
   The Step 2 probe runs AFTER consent, not before (it uses the key).

---

## Step 0: Key Intake and Preflight

1. **Runtime available:** `curl --version` (first line) and `python3 --version`
   (fall back to `python`, then `node`). If curl AND all script runtimes are
   missing → tell the user and exit cleanly.
2. **Explain the key requirement** (before asking for anything):
   "OpenAI usage discovery needs an **Admin API key** (`sk-admin-...`) — regular
   project keys (`sk-proj-...`) cannot read org usage. Create one at
   platform.openai.com → Settings → Organization → Admin keys. Grant it the
   **read-only** `api.usage.read` scope (usage and costs) — nothing else."
3. **Check the environment first** (presence only, never the value):

   ```bash
   [ -n "$(printenv OPENAI_ADMIN_KEY)" ] && echo ENV_KEY_PRESENT || echo ENV_KEY_ABSENT
   ```

4. **Key intake** — AskUserQuestion: "How would you like to provide the Admin
   key?" Options (offer the first only on `ENV_KEY_PRESENT`):
   - **Use the `OPENAI_ADMIN_KEY` already in my environment** → materialize env
     var to file in one command — the value never appears in the transcript:

     ```bash
     printf 'OPENAI_ADMIN_KEY=%s\n' "$(printenv OPENAI_ADMIN_KEY)" > "$MIGRATION_DIR/.openai-admin-env" && chmod 600 "$MIGRATION_DIR/.openai-admin-env"
     ```
   - **I'll write it to a file myself** → give the user this command to run in
     THEIR OWN terminal (not through the agent) — `read -rs` collects the key
     without echoing it:

     ```bash
     read -rs k && printf 'OPENAI_ADMIN_KEY=%s\n' "$k" > "<MIGRATION_DIR>/.openai-admin-env" && chmod 600 "<MIGRATION_DIR>/.openai-admin-env" && unset k
     ```

     Substitute the literal run-directory path when presenting it (the path is
     not a secret). Continue when the user says it's done.
   - **Skip OpenAI usage discovery** → exit cleanly with no output.
5. **Format check** (never prints the key):

   ```bash
   grep -qE '^OPENAI_ADMIN_KEY=sk-.+' "$MIGRATION_DIR/.openai-admin-env" && echo KEY_FORMAT_OK || echo KEY_FORMAT_BAD
   ```

   On `KEY_FORMAT_BAD`: re-run intake (do not echo file contents).

**IMPORTANT:** Do NOT rely on the environment variable during capture — env vars
do not persist across Bash tool calls. The capture script reads the file path
above.

## Step 1: Consent Gate

Output exactly, then wait for the user's choice:

```
─── OpenAI Usage Discovery (read-only) ───

I can pull your organization's OpenAI cost and usage data directly
from the OpenAI Admin API. This runs GET requests only, against a
fixed list of usage/cost endpoints:

  ✓ Captured: daily cost totals by line item, token counts per
    model (completions, embeddings, images, audio), request
    counts, and project names/IDs.
  ✗ Never captured: prompts or completions content, uploaded
    files, API keys, org members, or per-user/per-key attribution.
    No request that creates, changes, or deletes anything will run.

Window: last 30 days. Output is written to
.migration/<run>/openai-capture/ (gitignored). Your Admin key stays
in a chmod-600 file inside the gitignored .migration/ directory and
is never echoed or stored in any artifact.

[A] Proceed with OpenAI usage discovery
[B] Skip — use exported billing files only (or none)
```

- **[A]** → continue to Step 2.
- **[B]** → exit cleanly with no output (record the decline for the orchestrator).

## Step 2: Capture

Create `$MIGRATION_DIR/openai-capture/`.

**2a. Write the capture script** to `$MIGRATION_DIR/_capture_openai.py` (or `.js`
— whatever runtime Step 0 found). The script (and nothing else) touches the key:

- Reads `OPENAI_ADMIN_KEY` from `$MIGRATION_DIR/.openai-admin-env`.
- Sends `Authorization: Bearer <key>` on every request. Never prints the key or
  the header; on HTTP errors it prints ONLY the status code and the response
  `error.message`.
- Computes `start_time` = now − 30 days (Unix seconds).
- For each row of the Capture Endpoint Table: calls the endpoint, follows
  pagination (`has_more` / `next_page` cursor) until exhausted, concatenates
  all pages' `data` arrays, and writes the result to the named file.
- A non-200 on one endpoint records `failed` for that row and continues —
  a missing endpoint or zero usage is normal, never a halt.
- Prints one line per row: `<file> ok|failed|skipped <n_buckets>`.

**2b. Capture Endpoint Table.**

| # | Endpoint (GET, `https://api.openai.com`)                                                          | Query parameters                                            | Output file           |
| - | -------------------------------------------------------------------------------------------------- | ----------------------------------------------------------- | --------------------- |
| 1 | `/v1/organization/costs`                                                                            | `start_time`, `bucket_width=1d`, `group_by=line_item`, `limit=180` | `costs.json`          |
| 2 | `/v1/organization/usage/completions`                                                                | `start_time`, `bucket_width=1d`, `group_by=model`, `limit=180`     | `usage-completions.json` |
| 3 | `/v1/organization/usage/embeddings`                                                                 | `start_time`, `bucket_width=1d`, `group_by=model`, `limit=180`     | `usage-embeddings.json`  |
| 4 | `/v1/organization/usage/images`                                                                     | `start_time`, `bucket_width=1d`, `group_by=model`, `limit=180`     | `usage-images.json`      |
| 5 | `/v1/organization/usage/audio_speeches`                                                             | `start_time`, `bucket_width=1d`, `group_by=model`, `limit=180`     | `usage-audio-speeches.json` |
| 6 | `/v1/organization/usage/audio_transcriptions`                                                       | `start_time`, `bucket_width=1d`, `group_by=model`, `limit=180`     | `usage-audio-transcriptions.json` |
| 7 | `/v1/organization/projects`                                                                         | `limit=100` (names/IDs only — used to label per-project cost) | `projects.json`       |

Row 1 is the **probe**: run it first. On 401, stop capturing and tell the user:
"The key was rejected. Confirm it is an **Admin** key (`sk-admin-...`) with the
`api.usage.read` scope — project keys cannot read org usage." Offer to re-run
Step 0 intake or skip. On 429, wait 30 seconds and retry once.

**2c. Run the script, then delete it.** Record results in
`$MIGRATION_DIR/openai-capture/manifest.json`:

```json
{
  "captured_at": "<ISO 8601 UTC>",
  "window_days": 30,
  "admin_key_sha256": "<sha256 of .openai-admin-env contents — fingerprint only>",
  "captures": [
    { "endpoint": "<row endpoint>", "file": "<file>", "status": "ok|failed|skipped", "note": null }
  ]
}
```

Every attempted or deliberately skipped row gets an entry. If EVERY usage row
failed, exit with no output and tell the user which scope is missing.

## Step 3: Parse Captures into the Usage Profile

Sum across the window (a throwaway extraction script if captures are large):

- **Costs** (`costs.json`): total spend over the window; per-line-item totals
  (line items map to model families and endpoint types). `monthly_cost_usd` =
  window total scaled to 30 days of ACTIVE billing (if the org's first non-zero
  bucket is < 30 days old, scale from first-activity date and set
  `partial_window: true`).
- **Usage** (rows 2–6): per model — `input_tokens`, `output_tokens` (completions
  and embeddings; embeddings have no output tokens), `num_model_requests`,
  images/seconds counts for image/audio endpoints.

Write `$MIGRATION_DIR/openai-usage-profile.json`:

```json
{
  "metadata": {
    "report_date": "2026-08-21",
    "source": "openai_usage_api",
    "captured_at": "<from manifest>",
    "window_days": 30,
    "partial_window": false,
    "projects": [{ "id": "proj_abc", "name": "example-project" }]
  },
  "summary": {
    "monthly_cost_usd": 105.03,
    "currency": "USD",
    "models_seen": 5,
    "total_requests": 2856
  },
  "costs_by_line_item": [
    { "line_item": "gpt-5.6-terra, input", "monthly_cost_usd": 41.61 }
  ],
  "usage_by_model": [
    {
      "model": "gpt-5.6-terra",
      "endpoint_type": "completions|embeddings|images|audio_speeches|audio_transcriptions",
      "input_tokens": 1300000,
      "output_tokens": 145000,
      "num_model_requests": 452
    }
  ]
}
```

`usage_by_model` sorted descending by `input_tokens + output_tokens`. Include
only models with non-zero usage. Validate: valid JSON, `summary.monthly_cost_usd`
equals the sum of `costs_by_line_item` (± rounding).

## Step 4: Merge into the AI Workload Profile (if it exists)

If `$MIGRATION_DIR/ai-workload-profile.json` exists (from app-code or IaC
discovery), update it — the API data is authoritative for spend and volume:

1. `metadata.sources_analyzed.openai_usage_api` = `true`.
2. `current_costs` = `{ "monthly_ai_spend": <summary.monthly_cost_usd>,
   "services_detected": [<distinct endpoint_type values, prefixed "OpenAI ">],
   "source": "openai_usage_api" }`. If billing-CSV data already populated
   `current_costs`, do NOT silently overwrite: keep the LARGER spend value and
   record the other in `current_costs.conflicting_sources[]` — same
   never-silently-resolve rule as live-discovery drift.
3. Append to `detection_signals[]`:
   `{ "method": "openai_usage_api", "pattern": "billed usage for <model>",
   "confidence": 0.99, "evidence": "<N> requests, <X> tokens in last 30d" }`
   for each of the top 5 models by usage.
4. For any `usage_by_model` model absent from `models[]`: append
   `{ "model_id": "<model>", "service": "openai_api", "detected_via":
   ["usage_api"], "evidence": [{ "source": "usage_api", "pattern": "billed
   usage in last 30 days" }], "capabilities_used": [<from endpoint_type:
   completions→"text_generation", embeddings→"embeddings", images→"vision",
   audio_*→"audio">], "usage_context": "Observed in OpenAI usage data —
   call sites not yet located in code" }`. Code-derived entries always win on
   conflict; usage-only entries tell Clarify what code analysis missed.
5. If `summary.ai_source` is `"gemini"` and OpenAI usage was found, set it to
   `"both"`.

If `ai-workload-profile.json` does NOT exist, `openai-usage-profile.json`
stands alone — Clarify and Estimate read it directly.

Report: "OpenAI usage discovery: $X/month across N models (window: 30 days)."
Then tell the user: "Your Admin key is no longer needed — delete
`$MIGRATION_DIR/.openai-admin-env` now? [recommended]" and act on the answer.

The parent `discover.md` owns the phase status update — do not touch
`.phase-status.json` here.

---

## Error Handling

| Error                                             | Behavior                                                                                                       |
| ------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| curl / script runtime missing, or user skips      | Exit cleanly with no output (orchestrator falls back to billing files)                                          |
| 401 on probe                                      | Not an Admin key or missing `api.usage.read` scope — offer re-intake or skip                                    |
| 429 rate limit                                    | Wait 30s, retry once; second 429 → record `failed`, continue                                                     |
| Individual endpoint fails                         | Record `failed`/`skipped` in manifest, continue — zero usage on an endpoint is normal, never a halt             |
| Every usage endpoint failed                       | Exit with no output; tell the user which scope is missing                                                        |
| All buckets zero (new org, no usage yet)          | Write the profile with zeros and `partial_window: true`; warn that Estimate will fall back to token-volume tiers |

**Key principle:** partial results are better than no results. Record what failed;
never fabricate what wasn't captured.

## Scope Boundary

**This sub-file covers OpenAI usage capture ONLY.**

FORBIDDEN — Do NOT include ANY of:

- AWS service names, recommendations, or equivalents
- Migration strategies, phases, timelines, cost estimates, or effort estimates
- Any non-GET request, any endpoint not in the Step 2 table, any per-user or
  per-key grouping
- The Admin key value anywhere outside `.openai-admin-env` (no echoes, no
  command args, no artifacts, no context)

**Your ONLY job: capture what the org spent and used on OpenAI. Nothing else.**
