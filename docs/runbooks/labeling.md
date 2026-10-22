# Spender and need/want labels

`labeling` tags each spending row with whose spending it is: a native Wealthfolio event of
type **Spender**. Need or want is the category's budget group (Personal and Giving count as
Want). It never edits activity rows. After each daily sync, `python -m labeling apply` writes
account defaults, merchant labels, and model guesses above the threshold. Edits made in
Wealthfolio win.

## Labeler

Ask the agent to open the **Spending labeler** canvas (`spending-labeler`, input
`{"dataDir": "<your-finance-data>"}`). It writes through the API under the shared write lock,
after a native backup.

| Key | Action |
|---|---|
| Spender letter (from config) | Label the highlighted row; with **Shift**, the whole merchant, past and future |
| **Enter** | Accept the guess |
| **C** / **N** / **W** | Pick a category, or a Need or Want category with a suggested built-in one pre-selected (**Enter** accepts it; **Shift**: native rule for the merchant) |
| **'** · **U** · **↑↓** | Note · undo · move between rows |
| **→**/**Space** · **←** | Next card (asks once if the card is incomplete) · previous card |
| **M** · **Shift+M** | Rename the merchant (an existing name merges; blank resets) · rename only this row's raw name, to split it out |
| **O** · **Tab** · **R** · **Esc** | Cycle the queue order (remembered) · next view (**Shift+Tab** back) · re-read Wealthfolio · close a picker or box |

**Predictions** lists every unlabeled row with the model's guess and confidence. Sort by any
column and filter; **Enter** accepts a guess, and **Space** then **A** accepts the selected rows.
**Audit** filters labels by source and confidence; **H** shows a row's history.

**Rules** lists every rule: Wealthfolio's category rules (including presets), which Wealthfolio applies on
import, and the labeler's merchant spender labels and names, applied after each daily sync. Each card lists
the rules for its merchant. **X** twice removes one; a removed Wealthfolio rule can be restored with **U**.

A card stays on screen after labeling. It is complete when every row has a spender and a category
(which gives need or want); a merchant labeled by hand stays queued until it has both.

## Private files (`<data>/labels/`)

`config.json` (spenders and keys, threshold, account defaults, cardmember files,
check accounts, `deferMerchants` regexes that move merchants to the end of the queue,
`descriptionFiles` exports that supply descriptions for rows with empty notes and, with a `merchant`
column, rename suggestions),
`ledger.jsonl` (append-only reasons and history, including merchant names), `applied.json`, `model.json`,
`eval-<version>.json`, and the local model's `llm-proposals.jsonl` and `llm-eval-<model>.json`.
`python -m labeling status` summarizes them.

## Local-model categories

`python -m labeling llm` asks a local [Ollama](https://ollama.com) model for each uncategorized merchant's
category. The idea is TypeSafe's Jev (a typed choice plus its probability), but nothing leaves the machine:
the client accepts only loopback URLs and refuses cloud models. The model sees the merchant's descriptions,
payees, MCC, row count, typical amount, and account and activity types. It never sees dates, account names,
or existing categories. Its JSON schema lists only Need and Want categories plus `none`, so transfers, card
payments, and mixed merchants get `none`. Merchants with transfer rows are not asked at all. The answer
carries a confidence: the model's own probability of the category it wrote, from token log probabilities,
when the runner returns them for the whole reply. Otherwise it's the confidence the model reports.
Calibration happens per model. Answers are appended to `llm-proposals.jsonl` and reused until the
merchant, model, or prompt changes.

1. When the GPU is free, run `python -m labeling llm --eval [--model qwen3.8:27b]`. It asks about merchants
   whose rows all share one category (hand-set ones first) and fits a calibration. It scores accuracy,
   calibration, coverage, and precision on merchants held out from that fit, then writes
   `llm-eval-<model>.json` with the lowest threshold whose held-out precision is at least 0.95 on at least
   30 hand-categorized merchants (or none). Rule-set categories are scored and reported but never
   certify a threshold. Most of them came from another model's judgement, and the merchants still
   uncategorized are the hard ones. So until enough merchants are categorized by hand, nothing is
   applied automatically.
2. Run `python -m labeling llm` to propose categories for uncategorized merchants, largest dollars first
   (`--limit` caps a run).
3. Run `python -m labeling llm --apply --dry-run` to list what would be written, then `--apply`. Only
   proposals at or above the evaluated threshold are written, and only for merchants with no category
   on any row. Each becomes a native rule named `LLM: <key>`, recorded in the ledger as **Local LLM**
   with the model, confidence, and rationale. It takes a native backup first, then holds the write lock
   for one batch of 50 at a time. It refuses to run during the daily sync (05:45–06:30). `--undo`
   reverts the last batch.

With **N** or **W**, the labeler pre-selects the local model's category when the merchant has no category
yet. The hint shows the model, its calibrated confidence, and its rationale.

## Threshold

Label about 100 rows in the **Check set** tab, which shows shared-card rows without the guess
and never trains on them. Then try `threshold` values in `config.json` with
`python -m labeling apply --dry-run` and keep the lowest whose `checkSet.precisionAtThreshold` is at least 0.95. Until the check set has
`minCheckLabels` (default 30) confident labels at that precision, no prediction is applied automatically.