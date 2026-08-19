# Resolve Bedrock Model ID

Migration plans are authored ahead of execution. By the time the execute agent
runs, plan-supplied Bedrock inference-profile IDs may be stale, use the wrong
regional prefix (`us.` / `global.` / `eu.`), or never existed. This skill
takes an input ID, lists live profiles, and returns a validated ID — asking
the user to choose when the match is ambiguous.

## Input

- `plan_model_id`: the target_model_id from the migration plan
  (e.g., `anthropic.claude-sonnet-4-6-20250514-v1:0` — a plausible-looking ID
  that does NOT exist; broken inputs like this are exactly what this helper
  repairs, so this example is intentionally invalid)
- `region`: the AWS region from your context (e.g., `us-east-1`)

## Procedure

### Step 0: Route mantle-only models away from inference-profile resolution

**Check this before Step 1.** If `plan_model_id` matches `^openai\.gpt-5` (the proprietary GPT models — GPT-5.6
Sol/Terra/Luna, GPT-5.5, GPT-5.4), the inference-profile path below **cannot** resolve it and will always fail:

- These models are served only on the `bedrock-mantle` endpoint. They have no `bedrock-runtime` model and no
  inference profile, so `aws bedrock list-inference-profiles` never returns them.
- They do not support Geo or Global cross-region inference, so there is no `us.` / `global.` / `eu.` prefixed variant
  to rank against. The Step 3 token ranking would score every candidate at near-zero overlap and Step 4 would return
  `blocked: model_unresolvable` for an ID that is in fact perfectly valid.

For these IDs, validate against the mantle catalog instead:

```bash
aws bedrock list-foundation-models \
  --region <region> \
  <add --profile <profile> when your context has an `AWS profile` line> \
  --query "modelSummaries[?starts_with(modelId, 'openai.')].[modelId,modelName]" \
  --output json
```

- **Exact match** on `plan_model_id` → return it unchanged. Do not add a regional prefix and do not strip or append a
  `-v1:0`-style suffix; the mantle ID form is the literal `openai.gpt-5.6-terra` shape.
- **No match** → the model is not enabled or not available in this region. Return `blocked` with
  `reason: model_unavailable_in_region` and put the region plus the `openai.*` IDs that _were_ returned in `detail`,
  so the orchestrator can offer them. Because these models are in-region only, the remedy is a **region change or a
  different model** — never a cross-region inference profile.
- If the CLI call itself fails or the account lacks `bedrock:ListFoundationModels`, return `blocked` with
  `reason: model_unverifiable` rather than guessing.

Also note for the caller: these models need `bedrock-mantle:*` IAM actions (e.g. via
`AmazonBedrockMantleInferenceAccess`), not `bedrock:InvokeModel`. A resolution success here does not imply the caller
is authorized to invoke.

Non-`openai.gpt-5*` IDs continue to Step 1 unchanged.

### Step 1: List live inference profiles

```bash
aws bedrock list-inference-profiles \
  --region <region> \
  <add --profile <profile> when your context has an `AWS profile` line> \
  --query 'inferenceProfileSummaries[].[inferenceProfileId,inferenceProfileName]' \
  --output json
```

Parse the JSON. Each entry is a `[id, name]` pair.

### Step 2: Try exact match

If `plan_model_id` appears verbatim in the list, return it. No user prompt
needed.

### Step 3: Token-based ranking when no exact match

Tokenize both the plan ID and each live ID by splitting on `.`, `-`, `_`,
`/`. Drop tokens that match the regex `^v?\d{6,}` or `^v\d+$` (these are
date stamps like `20250514` or version tags like `v1`).

For each live profile, compute the size of the intersection of its token set
with the plan ID's token set. Keep the top 3 by intersection size, breaking
ties in this order:

1. Prefer profiles whose ID starts with `us.`
2. Then `global.`
3. Then no prefix
4. Then `eu.` / others

### Step 4: Defer to the orchestration skill

The subagent that loads this skill is non-interactive and cannot prompt the
user. When no exact match exists, return `blocked` with
`reason: model_unresolvable` and put the plan's ID and the top candidates in
`detail`, so the orchestration skill (main session) presents the choice. The
candidate-selection logic above (Steps 1-3) defines what the orchestrator
offers; format `detail` so it can render the choices:

```
The migration plan references Bedrock model '<plan_model_id>', but that ID is
not available in <region>. Closest matches found:
  - <candidate 1 id> (<candidate 1 name>)
  - <candidate 2 id> (<candidate 2 name>)
  - <candidate 3 id> (<candidate 3 name>)
The user may also supply a different inference profile ID, or abort to fix the
plan first.
```

Include fewer candidates if fewer exist. If zero candidates have token overlap

> 0, omit the candidate rows and note only that the user must supply a correct
> ID or abort.

### Step 5: Return

ONLY an exact match (Step 2) returns an ID directly. Token ranking (Step 3)
exists solely to produce the candidate list inside Step 4's `blocked` detail —
a token-ranked match is NEVER auto-applied, because silently substituting a
different model than the plan named would make every downstream eval and
rewrite target the wrong model without the user knowing. Anything short of an
exact match returns the `blocked` signal from Step 4 — the orchestration skill
asks the user and re-invokes resolution with the chosen (or pasted) ID, or
stops on abort.

## Notes

- This skill is idempotent: calling it twice with the same already-validated
  ID will hit Step 0 (mantle) or Step 2 (inference profile) and return immediately.
- Steps 1–5 assume the target is a `bedrock-runtime` model reachable through an
  inference profile. Mantle-only models are handled entirely in Step 0 and never
  reach the token-ranking logic. See
  `gcp-to-aws/references/shared/openai-on-bedrock.md` for the authoritative list
  of mantle-only model IDs and their regions.
- Output of this skill should replace the plan's `target_model_id` in the
  caller's context — downstream phases (evaluator, rewriter) receive the
  validated ID only.
