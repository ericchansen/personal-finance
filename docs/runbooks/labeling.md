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
| **C** / **N** / **W** | Pick a category, a Need category, or a Want category (**Shift**: native rule for the merchant) |
| **'** · **S** · **U** · **↑↓** | Note · skip · undo · move between rows |
| **O** · **Tab** · **R** · **Esc** | Cycle the queue order (remembered) · next view (**Shift+Tab** back) · re-read Wealthfolio · close a picker or box |

**Predictions** lists every unlabeled row with the model's guess and confidence. Sort by any
column and filter; **Enter** accepts a guess, and **Space** then **A** accepts the selected rows.
**Audit** filters labels by source and confidence; **H** shows a row's history.

## Private files (`<data>/labels/`)

`config.json` (spenders and keys, threshold, account defaults, cardmember files,
check accounts, `deferMerchants` regexes that move merchants to the end of the queue,
`descriptionFiles` exports that supply descriptions for rows with empty notes),
`ledger.jsonl` (append-only reasons and history), `applied.json`, `model.json`,
and `eval-<version>.json`. `python -m labeling status` summarizes them.

## Threshold

Label about 100 rows in the **Check set** tab, which shows shared-card rows without the guess
and never trains on them. Then try `threshold` values in `config.json` with
`python -m labeling apply --dry-run` and keep the lowest whose `checkSet.precisionAtThreshold` is at least 0.95.