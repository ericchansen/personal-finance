# Vanguard full-history planning

Vanguard's custom activity workbook is account-specific and contains the real
trade dates and quantities needed to reconstruct holdings. Keep workbooks,
account mappings, audit output, and generated plans outside this repository.

Generate a read-only plan:

```sh
python importers/extracts/vanguard_history_cli.py \
  --workbook-dir "<private download directory>" \
  --audit-json "<private workbook audit.json>" \
  --output "<private output directory>/vanguard-history-plan.json" \
  --data-dir "<private finance-data directory>"
```

The planner:

- treats VMFXX as cash and omits its internal sweep trades;
- converts a dividend/reinvestment source pair into a balanced DIVIDEND + BUY;
- preserves regular buys, sells, exchanges, contributions, and distributions;
- blocks unpriced in-kind transfers and recharacterizations instead of inventing
  basis or a unit price;
- excludes every unmapped workbook, including closed accounts;
- fingerprints workbooks, live activities, and the complete plan;
- flags any proposed deletion carrying `sourceGroupId`.

## Canonical normalized history

The normalized builder reads every archived activity workbook, not a generated
Wealthfolio migration plan. It applies the same classification and cited NAV
resolution semantics, then requires exact share and cash agreement with the
latest combined holdings export. The combined CSV transaction section is an
overlap assertion only and is deliberately ignored.

Every closed or otherwise unmapped workbook must be recorded under
`excludedAccounts` in the private `extracts/vanguard/mapping.json`, with a
stable `decision` and human-readable `reason`. Missing price resolutions,
incomplete mappings, or any reconciliation difference block the build.
Canonical output preserves internal transfer groups and distinguishes external
zero-cash in-kind flows. No canonical command reads from or writes to
Wealthfolio.

## Apply safety

Apply is enabled only for a blocker-free plan when the exact fingerprint is
supplied and live activities still match the plan snapshot. It creates a
database backup, performs creates/updates/deletions in one bulk mutation,
verifies returned mutation counts, links the internal recharacterization, and
triggers portfolio recalculation. A linked transfer must be explicitly unlinked and relinked
rather than deleted; otherwise Wealthfolio can cascade-delete its counterpart.
Preserve and verify any existing employer-plan-to-rollover paired transfer.

Rollback means stopping at the first failed mutation or reconciliation,
restoring the verified pre-migration database backup, and checking the preserved
transfer pair before retrying.
