# Local model categorization (Ollama)

Some merchants cannot be resolved by any deterministic method. A payee with no
reviewed canonical history *and* no existing Wealthfolio assignment is genuinely
novel — there is nothing local to look it up in. Before this, every one of those
was a manual decision.

This runbook covers the optional harness that asks a **locally running model**
what a novel merchant is, using an Ollama server bound to loopback. It is an
addition to [`categorization.md`](categorization.md); the deterministic pipeline
is unchanged and always runs first.

> **The privacy boundary.** The merchant description is the one piece of
> transaction data that identifies a person's behaviour. It is sent to exactly
> one destination — an Ollama server on `127.0.0.1` — and it is never written to
> disk, never logged, never cached, and never sent anywhere else. Everything
> this harness persists is keyed by the same HMAC-SHA256 merchant digest the
> sealed plan already uses.

## What makes this an agent rather than a prompt

The model is never handed a bare merchant string. Each question is assembled
from deterministic local tools, and the answer is re-checked against the same
guards the planner enforces:

| Tool | Evidence it contributes |
| --- | --- |
| `live_taxonomy` | Every selectable category in the live taxonomy, with parent nesting. These ids are the *only* legal answers. |
| `transaction_shape` | Cash-flow direction (debit/credit), activity types, account kinds. |
| `recurrence` | How many activities the merchant covers, over which months, in what amount band, and whether the amount repeats exactly. |
| `source_evidence` | Which importers produced the rows and why the planner left them unresolved. |
| `category_history` | What Wealthfolio's own assignments and the reviewed canonical estate already say about this exact merchant — including a *below-threshold* consensus the planner was not allowed to apply. |
| `structural_guard` | The structural verdict. Transfers, card payments, loan payments, savings, investment movements and reconciliation rows are removed **before** this point and are not overridable. |
| `merchant_research` | Optional, local-only, disabled by default. See below. |

Tool output is assembled locally and passed in the prompt. The model does not
call tools, cannot request more data, and cannot reach anything.

## Clustering: one decision per merchant, not per transaction

Unresolved activities are grouped by `(merchant digest, direction)`. A
subscription billed twelve times is **one** question, and all twelve instances
are decided identically by construction.

Direction is part of the key on purpose: a payee that appears as both a charge
and a refund is two different questions resolving against two different
taxonomies.

## Where the model sits in the decision order

The model is the **last** source consulted, and only for the three manual
reasons that mean *the deterministic layer ran out of evidence*:

- `no-history`
- `insufficient-history`
- `unmapped-category`

Everything else is deliberately out of scope:

- **Identity abstentions** (`unknown-source-identity`, `ambiguous-*`,
  `unmapped-account`, …) — there is nowhere to write a decision back to and
  nothing to verify at promotion time.
- **Structural exclusions** — a money movement is not a purchase, and no model
  opinion changes that.
- **`conflicting-history`** — reviewed evidence that disagrees with itself is
  exactly where a confident guess does damage. A human settles it.
- **`credit-direction-review`** — a cash-account credit with no income-side
  category. Add a `categoryAliases` entry instead.

So the full order becomes:

1. Structural guards (gap rows, transfers, card/loan payments, saving,
   investment, reconciliation, exclusions).
2. Private `category-decisions.json` overrides and reviewed schema v2 rules.
3. Reviewed canonical carryover.
4. Reviewed canonical merchant consensus.
5. Wealthfolio's own live category history.
6. **The local model — only for what 1–5 left unresolved.**

Implementation detail worth knowing: the plan is built **twice**. The first
build is fully deterministic and is what the agent reads; if suggestions are
applied, the plan is rebuilt from scratch with them available. The model is
therefore structurally incapable of pre-empting a deterministic decision.

## Check the model is available

```powershell
python -m importers.categorize.cli agent-health `
  --ollama-url http://127.0.0.1:11434 `
  --ollama-model qwen3:30b-a3b
```

Prints reachability, the server version, whether the model is installed, and
exits non-zero if the endpoint is not ready. It reads no private data, needs no
Wealthfolio session and writes nothing.

A non-loopback `--ollama-url` is refused outright.

## Produce suggestions (plan-only, assigns nothing)

```powershell
python -m importers.categorize.cli agent-plan `
  --data-dir D:\documents\finance-data `
  --base-url http://127.0.0.1:8088 `
  --start-date 2026-08-01 `
  --end-date 2026-08-31
```

This runs the ordinary deterministic plan, then asks the model about whatever is
left. It writes two private artifacts under `ollama-agent\`:

- `agent-suggestions-<stamp>.json` — every cluster, its assembled tool evidence,
  the decision, the confidence and the rationale.
- `agent-review-<stamp>.md` — the merchant-redacted Markdown companion.

`categoriesAssigned` in that artifact is `0`. Nothing is applied.

Useful flags:

- `--agent-min-confidence 0.90` — the bar a suggestion must clear to be
  applicable. Default `0.90`. Anything below stays manual review and is listed
  as such.
- `--agent-max-clusters N` — bound a run. The largest clusters are asked first,
  so a bounded run still covers the most activities.
- `--agent-max-attempts 3` — bounded self-correction (see below).
- `--no-agent-cache` — do not read or write the decision cache.
- `--ollama-model`, `--ollama-url`, `--ollama-timeout`, `--ollama-num-ctx`.

## Apply suggestions into a sealed plan

```powershell
python -m importers.categorize.cli agent-plan `
  --data-dir D:\documents\finance-data `
  --base-url http://127.0.0.1:8088 `
  --start-date 2026-08-01 `
  --end-date 2026-08-31 `
  --agent-min-confidence 0.90 `
  --apply-agent-suggestions
```

Only with `--apply-agent-suggestions` do high-confidence suggestions become
plan candidates. They then flow through the *same* staging rehearsal and
production promotion as every other candidate — see
[`categorization.md`](categorization.md#rehearse-and-promote). Nothing about
that path is relaxed for a model decision.

Applied candidates carry `evidenceKind: "ollama-agent"` and a
`liveHistory`-style seal at `ollamaAgent`:

```
ollamaAgent
├── endpoint              baseUrl, loopback: true, webResearchEnabled: false
├── model                 model, digest, parameterSize, quantization, fingerprint
├── promptSchemaFingerprint / promptTemplateVersion
├── minConfidence         the threshold that was actually enforced
├── toolManifest          which local tools produced the evidence
├── suggestions[]         evidenceHash, clusterId, merchantHash, categoryId,
│                         confidence, activityCount, sampleActivityIds,
│                         rationale, uncertaintyFlags
└── sealFingerprint
```

`validate_category_plan` refuses a plan whose seal:

- names a non-loopback endpoint, or one with web research enabled;
- does not identify its model by name **and** digest;
- has no valid confidence threshold, or applies a candidate below it;
- has a fingerprint that does not cover its own content;
- has a candidate citing evidence the seal does not contain, disagreeing with
  it field by field, or naming a different model digest.

## Validation, retry and abstention

Ollama's structured-output mode constrains decoding to a JSON schema. That
limits *shape*, never truthfulness, so every answer is re-validated locally and
rejected for any of:

| Rejection | Meaning |
| --- | --- |
| `malformed-json`, `not-an-object`, `missing-fields`, `unexpected-fields` | The output is not the agreed document. |
| `invented-category-id` | The id is not in the live taxonomy. |
| `direction-mismatch` | The taxonomy does not match the direction local evidence derived. |
| `confidence-not-finite`, `confidence-out-of-range` | `NaN`, `Infinity`, or outside `[0, 1]`. |
| `unknown-uncertainty-flag`, `unknown-rule-match-type`, `recommend-rule-invalid` | Vocabulary the harness does not accept. |
| `rationale-echoes-merchant`, `rationale-echoes-transaction-data`, `rationale-contains-long-number`, `rationale-contains-url`, `rationale-too-long`, `rationale-empty` | The free-text rationale would carry private data back out into an artifact. |

The rationale guard is deliberately narrow rather than blunt: a word the live
taxonomy itself publishes (`fuel`, `groceries`, `mortgage`) is allowed even when
the payee also contains it, because the plan already prints `categoryName` next
to the rationale. What is forbidden is the *distinctive* part of the payee —
the brand, the street, the franchise number — which is what would actually
identify the merchant. So "A fuel retailer." is accepted and "The name contains
'Nimbus'." is not.

A rejected answer is re-asked with the specific violation named **and an
actionable hint** ("name only the kind of business, as in 'A fuel retailer.'"),
up to `--agent-max-attempts` (default 3, i.e. two corrections). There is no
unbounded loop. A cluster that never validates is recorded as
`rejected:<reason>` and its activities stay manual.

`decision: "abstain"` is a first-class, expected outcome. An abstention carries
no category and no confidence. The prompt tells the model not to abstain merely
because a brand is unfamiliar — most bank descriptions are unfamiliar local
businesses whose trade is stated plainly in the name — but to abstain when the
description carries no interpretable signal at all.

A suggestion may also recommend a *transparent rule* (`recommendRule`,
`ruleMatchType`). No pattern is ever stored — that would be merchant text.
Derive the rule deterministically from the same history with
`python -m importers.categorize.cli merchant-rules`.

## The decision cache

Validated decisions are cached under `ollama-agent\cache\`, keyed by a hash of:

- the model fingerprint (name, digest, parameter size, quantization, server
  version),
- the prompt/schema fingerprint and prompt template version,
- the cluster's evidence hash.

Change the model, re-pull a moved tag, edit the prompt, alter the schema, or
change any tool output — including the taxonomy — and the key changes, so a
stale answer is never reused for a different question.

**A cache record contains no merchant text.** It stores hashes, ids and the
validated decision. That is asserted on write (the write is refused) and again
on read (the record is ignored and the decision re-derived), not assumed.

## Optional merchant research — local only, off by default

`--merchant-research local-file` reads an operator-curated JSON dictionary from
the private data directory, mapping a normalized payee to redacted facts:

```json
{
  "example merchant name": { "industry": "coffee shop" }
}
```

It is a plain local file read. There is no client and no socket.

**There is no web research backend, and adding one would be a privacy
decision, not a feature decision.** Looking a payee up on the internet would
publish the merchant name — and the timing of the query — to a third party, and
would tie a person's spending to a search provider's logs. If such a backend
were ever added it would have to be opt-in, off by default, documented as
sending merchant names off-machine, and it would still be refused by
`validate_category_plan`, which requires `webResearchEnabled: false` in the
seal.

## Where things live

Everything is under the private data directory, never the repository:

```
<data>/ollama-agent/agent-suggestions-<stamp>.json   full suggestion artifact
<data>/ollama-agent/agent-review-<stamp>.md          merchant-redacted review
<data>/ollama-agent/cache/<key>.json                 validated decision cache
<data>/ollama-agent/merchant-research.json           optional local facts (yours)
<data>/normalized/simplefin/category-plan-*.json     the sealed plan, as before
```

## Residual manual-review boundaries

The model narrows the manual pile; it does not eliminate it. These remain human
decisions by design:

- **Below-threshold suggestions.** A decision under `--agent-min-confidence` is
  reported with its rationale and uncertainty flags and applied to nothing.
- **Abstentions.** The model declined; the activity stays manual.
- **Rejected outputs.** A cluster that failed validation on every attempt.
- **`conflicting-history`.** Never offered to the model at all.
- **Every identity abstention.** No portable identity, no decision.
- **Structural rows.** Transfers, card and loan payments, saving and investment
  movements, balance-gap reconciliation and excluded activities.
- **`credit-direction-review`.** Cash-account credits with no income-side
  category.
- **Split transactions.** Still one category per activity.
- **The rationale is a claim, not a citation.** It is validated for privacy and
  length, not for truth. Sampling a few is worthwhile before applying a large
  pass for the first time.
- **A sealed decision is not re-derivable at promotion time.** Unlike live
  history, which is re-read for drift, a model decision is fixed evidence: the
  digest, prompt fingerprint and evidence hash prove *what was asked of which
  weights*, not that the same weights would answer identically today.
