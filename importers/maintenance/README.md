# Ledger maintenance

Corrections that are not imports. An importer brings a source of truth in; these
fix a ledger that no longer matches reality for reasons no export will explain.

## Rollovers

When a 401k is rolled into an IRA, the money exists twice: once in the old
employer plan, frozen at whatever balance the aggregator last saw, and once in
the new account holding the real positions.

```sh
python cli.py --source "Old Employer" --destination "Rollover IRA" \
    --date 2025-10-07 --dry-run
```

Deleting the old account would erase the years it was real, so its balance is
transferred out on the day the rollover settled and the account is deactivated.

The destination's opening entry is re-dated to the same day. Without that the
money leaves one account months before it arrives in the other, and the net
worth chart shows a hole that never existed. Pass `--no-redate` to skip this.

### Finding the date

Use the date the receiving institution recorded the incoming rollover, not the
date you noticed. It appears in the destination's transaction export as a
`Rollover (incoming)` or `Rollover Contribution` row. A trailing true-up
contribution weeks later is normal and does not change the settlement date.

### What it does not fix

The rolled-over balance is whatever the ledger held, which for a stale
aggregator connection is lower than the real amount at rollover — contributions
and growth in between were never recorded. Closing the account is still correct:
the destination is reconciled against a real export, so the *total* ends up
right even though the intervening months are understated.

Accounts are matched on a case-insensitive substring of the name. An ambiguous
match is refused rather than guessed at.

## Staleness report

```sh
python status_cli.py --threshold 45
```

Every source here is a manual export, so the ledger decays silently: an account
stops being refreshed and simply keeps reporting the last figure it saw. The
balance still looks like a balance. Nothing surfaces that on its own.

## Aggregate basis repair

`basis_repair_cli.py` corrects synthetic BUY prices when an institution reports
only current aggregate basis. Its private spec and generated plan must remain
under the configured data directory. The plan fingerprints every source file
and activity, preserves activity dates and quantities, and adjusts one explicit
funding entry by the same basis delta so cash and current value do not move.
For retirement plans with contribution history, `mode: "lot-replacement"` is
preferred: it replaces a synthetic aggregate BUY with one source-dated,
exact-amount funding/BUY pair per contribution and refreshes the manual quote.

Generate and apply the plan against a separately hosted copy of production
first. Applying to the default production port is refused unless
`--allow-production` is explicit. Apply also re-hashes every evidence file and
verifies the plan's canonical SHA-256. A successful rehearsal writes a receipt
containing that hash, base URL, and environment ID; production additionally
requires the receipt and the hash via `--expected-plan-sha256`.

The command backs up immediately before mutation. Lot creates and synthetic
deletes use one bulk request, with complete compensation for any partial
result. Linked deletes are refused. Manual quote state is fingerprinted during
planning, rechecked before mutation, and retained for rollback.

If the initial mutation loses its response, the command never retries it.
Instead, it rereads live activities, classifies committed changes by guarded
IDs, fingerprints, and idempotency keys, compensates only those changes, and
verifies the original fingerprints and account state. If inspection or
verification is inconclusive, it stops with an explicit database-backup
restore requirement.

Accounts are split by what a gap actually costs:

- **Cash flow at risk** — cash and credit accounts feed spending reports, so a
  gap removes real income and spending and makes the remaining months look
  better or worse than they were.
- **Net worth only** — investment accounts are excluded from spending reports,
  so a gap there misstates net worth but leaves cash flow intact.

Accounts that have never held an activity are listed separately. Those are
usually shells an aggregator created for something that was never real, and are
worth deactivating rather than chasing.

## Resolving duplicated accounts

An aggregator that reconnects to a reissued card often creates a **second
account** rather than continuing the first. Both then appear, one active and one
frozen at its last balance — so the same debt is counted twice, and during the
window when both connections were live, the same transactions are counted twice
in spending reports as well.

Telling this apart from a genuinely different card requires evidence, not a
guess. Compare the two histories:

- **Overlapping** date ranges with a high (date, amount) match rate suggests two
  connections to one card.
- **Sequential** ranges mean a reissue: the old card was really replaced, and
  its final payoff was simply never captured.
- A **long** overlap with a moderate match rate is ambiguous. Two cards used at
  the same dominant merchant match by coincidence. Establish a baseline by
  measuring the match rate between two accounts known to be unrelated; if that
  is near zero and the suspected pair is far above it, the pairing is real.

Remediation is surgical, not wholesale. The inactive account usually holds
**genuine history from before the active one begins**, so deleting the account
destroys real data. Delete only its activities dated on or after the handoff
date, then reconcile whatever balance remains to zero.

### ⚠️ Deleting a linked transfer deletes its counterpart too

An activity belonging to a matched transfer pair carries a `sourceGroupId`, and
deleting it **cascades to the other leg** — in whatever account that leg lives.

This is easy to trigger and hard to notice. Removing duplicated card rows can
also remove linked checking-account legs. The delete reports success and the
card balance can still look correct even though another account changed.

Unlink first, and re-link the surviving leg where it actually belongs:

```http
POST /activities/unlink   {"activityAId": "...", "activityBId": "..."}
POST /activities/link     {"activityAId": "...", "activityBId": "..."}
```

Both ids are required; `null` is rejected. Find the counterpart by matching
`sourceGroupId` across all activities — a group holds exactly two legs.

**Any bulk delete should snapshot cash balances first and abort if they move.**
That guard is what caught this, one step after the damage rather than never.

### Zeroing a credit card

Credit cards reject `TRANSFER_OUT` and `DEPOSIT`. To *reduce* a card's debt use
`TRANSFER_IN` with `subtype: "external_transfer"`, which moves the balance
without claiming income. To reduce a **credit** balance there is no
transfer-shaped option, so `WITHDRAWAL` is the only choice and it does register
as spending — keep the amount small enough that this does not matter, or leave
the residual alone.

```sh
python gap_cli.py --account "Example Checking" --to 100.00 --date 2026-01-31 \
    --note "Synthetic gap example" --dry-run
```

When an export starts after the ledger's last known activity, the balance can
still be corrected by adding the difference — but **how** it is added matters. A
`DEPOSIT` into a cash account is reported as income, so plugging a gap that way
inflates earnings by the size of the plug. Transfers are used instead: they move
the balance without claiming money was earned or spent.

This is a stopgap. The honest fix is to download the missing period. The entry
is therefore labelled with what is missing and dated to the day before the
export begins, so it stays out of the months that do have real data.

## Filling a known gap
