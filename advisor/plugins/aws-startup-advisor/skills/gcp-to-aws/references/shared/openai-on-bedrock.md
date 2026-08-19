# OpenAI Models on Amazon Bedrock

**Last verified:** 2026-08-10
**Sources:** [OpenAI model cards](https://docs.aws.amazon.com/bedrock/latest/userguide/model-cards-openai.html) (per-model
cards linked below), [GPT-5.6 launch post](https://aws.amazon.com/blogs/machine-learning/get-started-with-openai-gpt-5-6-sol-terra-and-luna-on-amazon-bedrock/),
[GPT-5.6 GA announcement](https://aws.amazon.com/about-aws/whats-new/2026/07/openai-gpt-sol-terra/),
[GPT-5.6 pricing update](https://aws.amazon.com/about-aws/whats-new/2026/07/openai-gpt-terra-luna-pricing-bedrock/)

OpenAI's **proprietary** models are available on Bedrock, not just the open-weight `gpt-oss` family. This changes the
default shape of every OpenAI → AWS migration: the source model itself is frequently a Bedrock target, so a
cross-family swap to Claude/Nova is no longer the only option — and is no longer the default.

**This file is the single source of truth for OpenAI-on-Bedrock facts in this plugin.** `ai-openai-to-bedrock.md`
(mapping policy), `design-ai.md` (selection), `estimate-ai.md` (costing), and `ai-migration-guardrails.md` (quota
risk) all defer to it. Do not restate model IDs, regions, or endpoint paths elsewhere — link here.

---

## Model Catalog

| Model             | Model ID (mantle)                        | Launched     | Context | Lifecycle | Model card                                                                                       |
| ----------------- | ---------------------------------------- | ------------ | ------- | --------- | ------------------------------------------------------------------------------------------------ |
| GPT-5.6 Sol       | `openai.gpt-5.6-sol`                     | Jul 13, 2026 | 1M      | Active    | [card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-56-sol.html)   |
| GPT-5.6 Terra     | `openai.gpt-5.6-terra`                   | Jul 13, 2026 | 1M      | Active    | [card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-56-terra.html) |
| GPT-5.6 Luna      | `openai.gpt-5.6-luna`                    | Jul 13, 2026 | 1M      | Active    | [card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-56-luna.html)  |
| GPT-5.5           | `openai.gpt-5.5`                         | Jun 1, 2026  | 272K    | Active    | [card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-55.html)       |
| GPT-5.4           | `openai.gpt-5.4`                         | Jun 1, 2026  | 272K    | Active    | [card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-54.html)       |
| gpt-oss-120b      | `openai.gpt-oss-120b`                    | Aug 5, 2025  | 128K    | Active    | open-weight; also on `bedrock-runtime` as `openai.gpt-oss-120b-1:0`                              |
| gpt-oss-20b       | `openai.gpt-oss-20b`                     | Aug 5, 2025  | 128K    | Active    | open-weight; also on `bedrock-runtime` as `openai.gpt-oss-20b-1:0`                               |
| GPT OSS Safeguard | `openai.gpt-oss-safeguard-120b` / `-20b` | —            | —       | Active    | content-moderation / guardrail enforcement, not general chat                                     |

**Naming:** GPT-5.6 uses generation number + capability tier. `Sol` = flagship reasoning, `Terra` = balanced
production, `Luna` = high-volume / low-latency. Tiers advance on independent cadences, so a future `Terra` may not
share a generation with a future `Sol`.

> **Context-window conflict (resolved):** the GPT-5.6 launch blog states 272K for all three variants; all three
> model cards state 1M. **The model cards are authoritative** — use 1M for GPT-5.6. GPT-5.5 and GPT-5.4 are 272K on
> both sources. Re-check on refresh; if AWS corrects the blog, the cards still win.

**Not on Bedrock (as of this refresh):** GPT-4o, GPT-4.1, GPT-4 / GPT-4 Turbo, GPT-3.5 Turbo, the o-series
(o1/o3/o4-mini), GPT-5 / GPT-5.1 / GPT-5.2, and the `*-Pro` variants (GPT-5.5 Pro, GPT-5.4 Pro). Sources whose model
is on this list have no same-model landing target — see `ai-openai-to-bedrock.md` for the two-option path.

---

## Access Path — `bedrock-mantle` Only

All five proprietary GPT models are reached through the **OpenAI Responses API on the `bedrock-mantle` endpoint**:

```
https://bedrock-mantle.{region}.api.aws/openai/v1
```

Three constraints that break naive assumptions:

1. **The path is `/openai/v1/responses`, not `/v1/responses`.** Every GPT model card carries this note explicitly:
   the OpenAI models sit on a different path from other models served on the mantle responses endpoint. Code that
   hardcodes `/v1` against a GPT model ID will 404.
2. **There is no `bedrock-runtime` / Converse path for these models.** Each model card's Programmatic Access table
   lists exactly one row, and it is `bedrock-mantle`. Only the open-weight `gpt-oss` models also expose
   `bedrock-runtime` (Converse / InvokeModel). This removes the usual "fall back to Converse for dedicated
   throughput or Bedrock-native features" escape hatch.
3. **No cross-region inference.** Geo inference ID and Global inference ID are both "Not supported" on every GPT
   model card. These are in-region only — there is no CRIS inference profile, and `bedrock:ListInferenceProfiles`
   will not return them (see `llm-to-bedrock` → `resolve-bedrock-model-id`).

**Chat Completions is unverified for these models.** The docs' API-compatibility matrix renders its support marks as
empty cells, so it cannot be read programmatically or by eye. Every AWS code sample and the launch post use
**Responses** exclusively, and no GPT model card lists Chat Completions among supported features. Treat Responses as
the only verified surface; if a source app is built on Chat Completions, plan for a reshape to Responses and probe
the target account before committing.

### Client setup

Requires the OpenAI SDK at **>= 2.45.0**. Preferred client auto-refreshes a short-term Bedrock token:

```python
from aws_bedrock_token_generator import provide_token
from openai import BedrockOpenAI

region = "us-east-1"
client = BedrockOpenAI(
    aws_region=region,
    bedrock_token_provider=lambda: provide_token(region=region),
    max_retries=6,
)

response = client.responses.create(
    model="openai.gpt-5.6-terra",
    input="...",
    reasoning={"effort": "medium"},
)
```

The alternative — `OpenAI(base_url=".../openai/v1", api_key=os.environ["AWS_BEARER_TOKEN_BEDROCK"])` — uses a key
that expires within 12 hours and is not refreshed. Do not recommend it for production.

**IAM:** the managed policy `AmazonBedrockMantleInferenceAccess` grants what inference needs, including
`bedrock-mantle:CreateInference` and `bedrock-mantle:CallWithBearerToken`. Note these are `bedrock-mantle:*` actions
— an IAM policy scoped only to `bedrock:InvokeModel` will not authorize these models.

**Reasoning effort:** all five accept `none`, `low`, `medium`, `high`, `xhigh`, `max`. Because these models reason
before responding, the model's output items (which may include reasoning items) must be passed back in the next
request for multi-turn and tool-calling flows.

---

## Regional Availability

In-region only. This is the tightest constraint in the whole path — every other Bedrock target in this plugin is
available in more regions than these.

| Model         | us-east-1 | us-east-2 | us-west-2 | us-gov-west-1 |
| ------------- | --------- | --------- | --------- | ------------- |
| GPT-5.6 Sol   | yes       | yes       | —         | —             |
| GPT-5.6 Terra | yes       | yes       | yes       | —             |
| GPT-5.6 Luna  | yes       | yes       | yes       | —             |
| GPT-5.5       | yes       | yes       | —         | —             |
| GPT-5.4       | yes       | yes       | yes       | yes           |

If the migration's target region is not in this table for the selected model, the same-model path is **unavailable**
— there is no cross-region fallback. Either change the target region or take the cross-family path.

---

## Pricing

Read off the [Bedrock pricing page](https://aws.amazon.com/bedrock/pricing/) OpenAI tab, 2026-08-14. On-demand,
in-region, US East (N. Virginia) and US East (Ohio) — identical in both; US West (Oregon) carries Terra, Luna and
GPT-5.4 only.

**Bedrock is NOT at parity with OpenAI's standard list price.** The page states: _"In-region inference is priced at
parity with OpenAI **data residency tier**."_ Against OpenAI's standard tier every rate below is exactly **1.10×**.
So a same-model move is **not** cost-neutral for a customer on OpenAI standard — it is about **10% more expensive**,
and the case rests on AWS commitments, governance, residency, and prompt caching rather than on price. State that
honestly; do not describe it as free.

### Short context window (272K)

| Model         | Input $/1M | Output $/1M | Cache write $/1M (30m) | Cache read $/1M |
| ------------- | ---------- | ----------- | ---------------------- | --------------- |
| GPT-5.6 Sol   | 5.50       | 33.00       | 6.875                  | 0.55            |
| GPT-5.6 Terra | 2.20       | 13.20       | 2.75                   | 0.22            |
| GPT-5.6 Luna  | 0.22       | 1.32        | 0.275                  | 0.022           |
| GPT-5.5       | 5.50       | 33.00       | —                      | 0.55            |
| GPT-5.4       | 2.75       | 16.50       | —                      | 0.275           |

### Long context window (1M) — GPT-5.6 only

| Model         | Input $/1M | Output $/1M |
| ------------- | ---------- | ----------- |
| GPT-5.6 Sol   | 11.00      | 49.50       |
| GPT-5.6 Terra | 4.40       | 19.80       |
| GPT-5.6 Luna  | 0.44       | 1.98        |

**The 1M context window is a separate, more expensive tier — input 2.0× and output 1.5× the short-context rate.**
Quoting the 1M context window as a capability without pricing the workload at this tier understates cost for any
long-context use case. GPT-5.5 and GPT-5.4 have no long-context tier at all (dashes on the page), so their usable
window is 272K.

**Not yet published:** global cross-region inference pricing. Do not estimate it.

**GovCloud differs:** GPT-5.4 in GovCloud (US-West) is 3.30 input / 0.33 cached input / 19.80 output.

> **Two source conflicts to be aware of.** The AWS News Blog for the July 30 repricing quotes Luna at
> **0.20 / 1.20** — the OpenAI standard-tier figure, not the Bedrock in-region rate on the pricing page (0.22 / 1.32).
> Prefer the pricing page. Separately, the **AWS Price List API carries no GPT-5.x rows at all**: a `get_pricing`
> query against `AmazonBedrock` returns `gpt-oss` and GPT OSS Safeguard only, and filtering on `GPT-5` or a `gpt-5`
> usage type returns zero rows (price-list publication 2026-08-04). The `awspricing` MCP therefore cannot price these
> models, and an empty result must not be read as "model unavailable."

### Prompt caching — GPT-5.6 only

Listed as a supported feature on the Sol, Terra, and Luna model cards. The GPT-5.5 and GPT-5.4 cards list
client-side tool calling in that slot instead and do **not** list prompt caching. Do not assume caching on 5.5/5.4.

| Property          | Value                                                        |
| ----------------- | ------------------------------------------------------------ |
| Cached input read | 90% discount vs uncached input                               |
| Cache write       | 1.25x the uncached input rate                                |
| Minimum prefix    | 1,024 tokens (below this nothing caches, `cached_tokens`= 0) |
| Breakpoints       | up to 4 per request                                          |
| Retention         | at least 30 minutes                                          |
| Modes             | implicit (on by default) and explicit (cache breakpoints)    |

Explicit mode uses `prompt_cache_options={"mode": "explicit"}` plus a `prompt_cache_breakpoint` on the content block
ending the reusable prefix; a stable `prompt_cache_key` improves match reliability. Cached input tokens **do not
count against the input-TPM quota**, which compounds the benefit at scale.

---

## Quotas

Inference on `bedrock-mantle` is governed by **two per-model, per-region quotas: input tokens per minute and output
tokens per minute. There is no requests-per-minute quota.** Exceeding a TPM quota returns HTTP 429.

This corrects two claims that were previously applied to all Mantle traffic in this plugin:

- There is **no shared 10,000 RPM account limit** governing these models — the quota dimension is TPM, per model,
  per region.
- "Switch to `bedrock-runtime` for dedicated throughput" is **not an available remedy**, because these models have
  no `bedrock-runtime` path at all (see Access Path above).

The supported mitigations are: exponential backoff with a bounded retry count (`max_retries` on the OpenAI SDK),
spreading load across minutes rather than bursting, ramping request rate gradually, and prompt caching (cached input
is exempt from the input-TPM quota). For sustained volume beyond that, pursue a quota increase.

Service tiers Standard / Priority / Flex / Reserved are listed on the model cards, but the tier-support marks render
as empty cells; the launch post states GPT-5.6 on-demand runs on **Standard**. Verify tier availability per model
before recommending Flex or Reserved for cost reduction.

---

## Features With No Bedrock Equivalent

These are the remaining legitimate reasons to keep a workload on OpenAI's own API. Cost is no longer one of them.

| OpenAI capability                                                           | Status on Bedrock                                                 |
| --------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| Realtime API                                                                | No equivalent                                                     |
| Image generation (gpt-image)                                                | Not an OpenAI model on Bedrock; use Stability AI (see lifecycle)  |
| Whisper (STT) / TTS                                                         | Amazon Transcribe / Polly — different service, API, pricing model |
| Embeddings (`text-embedding-3-*`)                                           | No OpenAI embedding model on Bedrock; use Titan Embeddings v2     |
| Assistants API with file search, vector stores, code interpreter            | No direct equivalent — see the decision tree in the mapping guide |
| A model not in the catalog above (GPT-4o, o-series, `*-Pro`, GPT-5/5.1/5.2) | No same-model target; cross-family or upgrade required            |

**Data handling:** these are third-party models under OpenAI terms. Classifier-flagged traffic is retained up to 30
days for automated abuse detection; retained inputs/outputs are stored and processed by AWS and not shared with
OpenAI unless the customer opts in. Prompts and completions are not used to train models. Calls run under the
customer's IAM policies, inside their VPC, logged to CloudTrail, and in-region inference keeps data in-region.

**Codex on Bedrock is GA** with pay-per-token pricing, inference through Bedrock, and usage counting toward AWS
commitments — relevant when the source workload is a coding agent.

---

## Refresh Checklist

This model family is moving fast (two GA waves and a repricing inside 10 weeks). On each refresh:

1. Re-read the [OpenAI model card index](https://docs.aws.amazon.com/bedrock/latest/userguide/model-cards-openai.html)
   for models added or removed, and each per-model card for lifecycle state and EOL date.
2. Recheck the region matrix — every model here is in-region only, so region changes are migration-blocking.
3. Recheck rates on the Bedrock pricing page OpenAI tab, and resolve any row still marked _unverified_.
4. Recheck whether the Price List API has gained GPT-5.x coverage; if it has, drop the caveat above and let
   `estimate-ai.md` price these models from the MCP.
5. Recheck whether Chat Completions and `bedrock-runtime` support have been added or clarified.
6. Feed any lifecycle change into `ai-model-lifecycle.md` and any rate change into `pricing-cache.md`.
