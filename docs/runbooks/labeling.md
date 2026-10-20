# Spender and need/want labels

Two questions about every spending row: **whose spending is it** (one person, the other, or
joint), and **is it a need or a want**. The answers live in Wealthfolio, so its own filters,
Insights, and Budget pages show them. The `labeling` package fills them in and records why.

## Where labels live

| Question | Stored in Wealthfolio as | Where you see it |
|---|---|---|
| Spender | A spending event of type **Spender** (one event per person plus *Joint*, covering all dates) | Transactions filter, Insights event totals |
| Need or want | The category's **budget group** (Needs, Wants; Personal and Giving count as want, Other is unset) | Budget page |
| Category | Native category assignment; merchant-wide changes are native categorization rules | Everywhere |

Wealthfolio 3.10 only accepts its own taxonomies on activities, so the spender cannot be a
custom taxonomy. Events can be, at the cost of the single event slot: a row tagged with a
spender cannot also carry a trip event. To make one row a need inside a want category, change
its category (or create a subcategory in Needs).

Labels never edit activity rows. Amounts, dates, transfer links, and balances cannot change.

## Provenance

`<data>/labels/ledger.jsonl` is an append-only log of every decision: the row or merchant, the
value, the source, an optional one-line note, who made it and when. Model entries also record
the calibrated confidence, the model version, and the features that drove the guess (for
example `merchant BLUE KETTLE (seen 14x as <person>)`). An undo appends a `revert` entry, so
overrides stay auditable. The current label is the newest entry from the strongest source:

1. `user` (in the labeler) or `wealthfolio-ui` (an edit made in Wealthfolio, detected on the next run)
2. `merchant` (a merchant-wide label; covers past and future rows)
3. `cardmember` (an issuer file names the cardmember)
4. `account` (the default for a personal account, with optional description patterns)
5. `model`, only at or above the configured confidence threshold

Model guesses below the threshold are never written; those rows stay untagged in Wealthfolio
and wait in the labeler queue.

## Private configuration

`<data>/labels/config.json` (never in this repository):

```json
{
  "spenders": {"e": "<person 1>", "j": "<person 2>", "b": "Joint"},
  "threshold": 0.9,
  "needWant": {"needs": "Need", "wants": "Want", "personal": "Want", "giving": "Want"},
  "accounts": {
    "<personal account id>": {"default": "<person 1>"},
    "<checking account id>": {"default": "<person 1>", "patterns": {"(?i)\\b(ATM|CASH WITHDRAWAL)": "Joint"}}
  },
  "cardmembers": [{"accountId": "<card id>", "file": "raw/card-evidence/<date>/<file>.qfx",
                   "patterns": {"(?i)<cardmember name>": "<person>"}}],
  "checkAccounts": ["<shared card ids>"],
  "checkSize": 100
}
```

The keys of `spenders` are the labeler's keyboard shortcuts. Shared cards get no account
default. `setup` adds the Wealthfolio event IDs.

## Commands

Run on the host with `FINANCE_DATA` set (or pass `--data-dir`). Writes take the shared lock
`<data>/maintenance/wealthfolio-write.lock` and skip when another writer holds it.

```powershell
python -m labeling setup    # once: native backup, Spender event type and events, first apply
python -m labeling apply    # reconcile Wealthfolio edits, retrain, write labels, save an evaluation
python -m labeling apply --dry-run
python -m labeling status   # labels by source and confidence band, plus the latest evaluation
```

The `sync` container runs `apply` after every daily sync that did not crash. Until
`config.json` exists it does nothing.

## Labeler canvas

In the Copilot app, ask the agent to open the **Spending labeler** canvas (`spending-labeler`,
input `{"dataDir": "<your-finance-data>"}`), or call its `progress` action. The canvas starts
`python -m labeling serve` from this checkout, takes a native database backup before its first
write, and writes through the API under the shared lock.

- **Queue**: one merchant at a time, highest (uncertainty × dollars) first, with the model's
  guess, its confidence, and the reasons. Keys: the spender letters label the highlighted row,
  and with **Shift** they label the whole merchant, past and future. **Enter** accepts the guess,
  **C** picks a category (Shift+C makes it a native rule for the merchant), **N** or **W** picks
  a Need or Want category, **'** adds a note, **S** skips, **U** undoes, and the arrow keys move between rows.
- **Check set**: a fixed random sample of shared-card rows, shown without the guess. These labels
  are never trained on; they give the honest accuracy number.
- **Audit**: filter labels by source and confidence (for example model labels below 0.95),
  relabel with the same keys, and press **H** for a row's full history.

## Model and evaluation

Naive Bayes over the merchant, description words, MCC and time of day (SimpleFIN rows only),
category, amount band, and weekday; standard library only. It trains on every non-model label.
Account and cardmember labels count 0.3, because they show who paid rather than whose spending
it was. It learns *Joint* only from your labels. Confidence is calibrated on merchant-held-out
folds. Each `apply` writes `eval-<version>.json`:

- `unseenMerchants.*`: accuracy on merchants the model never saw
- `laterPeriod`: trained on the oldest 80%, scored on the newest 20%
- `checkSet`: accuracy, coverage, and precision at the threshold on the blind shared-card sample.
  Once about 100 check rows are labeled, set `threshold` to the lowest value whose precision is
  at least 0.95.

## Recovery

Every change is logged in the ledger. To undo a session, press **U** repeatedly in the
labeler. To roll back completely, restore the native backup taken when the session started
(Wealthfolio Settings, or `<data>/wealthfolio/backups/`), following the
[sync runbook](local-simplefin.md#deploy).
