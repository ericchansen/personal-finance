# Wealthfolio mutation containment

All Wealthfolio mutations initiated by this repository are disabled by default.
The shared REST client classifies every request immediately before transport:
`GET` requests, known read-only `POST` requests, login, and database backup
creation remain available; every other request requires this exact opt-in:

```text
WEALTHFOLIO_MUTATIONS_ENABLED=true
```

The value is case- and whitespace-sensitive. Values such as `1`, `yes`, `TRUE`,
or `true ` do not enable mutations. A denied request names the HTTP operation
and route it refused.

## Guarded commands

Arguments that identify private files, accounts, plans, environments, or
fingerprints are shown as placeholders and must remain outside this repository.

| Command | Mutation family |
|---|---|
| `python -m importers.monarch.cli apply ...` | Account create/update and activity create |
| `python -m importers.monarch.cli link ...` | Transfer link |
| `python -m importers.monarch.cli balances ...` | Opening-balance activity create |
| `python -m importers.monarch.cli external ...` | Activity metadata update |
| `python -m importers.extracts.cli apply ...` | Generic extract activity create |
| `python -m importers.extracts.fidelity_cli ...` without `--dry-run` | Account, holding, and reconciliation activity create |
| `python -m importers.extracts.vanguard_cli ...` without `--dry-run` | Account, holding, and reconciliation activity create |
| `python -m importers.extracts.vanguard_history_cli ... --apply` | Activity create/update/delete, transfer link, and recalculation |
| `python -m importers.assets.cli ...` without `--dry-run` | Alternative asset and liability create |
| `python -m importers.maintenance.cli ...` without `--dry-run` | Rollover activity create/update and account deactivation |
| `python -m importers.maintenance.gap_cli ...` without `--dry-run` | Gap-repair activity create |
| `python -m importers.maintenance.basis_repair_cli ... --apply` | Basis activity replacement/update and quote import/restore |
| `python -m importers.rebuild.account_cli ... --apply` | Staging account create/update |
| `python -m importers.rebuild.asset_cli ... --apply` | Staging alternative asset/liability create |
| `python -m importers.rebuild.cli ... --apply` | Staging activity delete/update and transfer unlink/link |
| `python -m importers.rebuild.receipt_repair_cli apply ...` | Receipt-bound duplicate deletion, assignment preservation, and reconciliation update on an isolated clone |
| `python -m importers.rebuild.bounded_promotion_cli execute ...` | Separately authorized, exact bounded production database replacement |
| `python -m finance_store.incremental_cli apply ...` or `run ...` | Scope/instance/release-bound activity creates through the durable journal; existing corrections remain held |
| `python -m importers.rebuild.current_cli ledger ... --apply` | Staging ledger activity create and recalculation |
| `python -m importers.rebuild.current_cli assertions ... --apply` | Staging assertion activity create and recalculation |
| `python -m importers.rebuild.current_cli quotes ... --apply` | Staging quote import and recalculation |
| `python -m importers.rebuild.current_cli rollovers ... --apply` | Staging rollover activity create/update, transfer link, and recalculation |
| `python -m importers.rebuild.current_cli health ... --apply` | Staging health-issue dismissal |
| `python -m importers.rebuild.vanguard_cli ... --apply` | Staging activity create/update/delete, transfer link, and recalculation |
| `python -m importers.simplefin.apply_cli apply ...` | Staging activity create/update, transfer link/unlink, and recalculation |
| `python -m importers.simplefin.apply_cli promote-apply ...` | Production activity create/update, transfer link/unlink, and recalculation |
| `python -m importers.simplefin.categorize_cli rehearse ...` | Staging category assignment |
| `python -m importers.simplefin.categorize_cli promote ...` | Production category assignment |
| `python -m importers.simplefin.categorize_cli budget-promote ...` | Production budget target create/delete |
| `python -m importers.categorize.cli rehearse ...` | Staging category assignment |
| `python -m importers.categorize.cli promote ...` | Production category assignment |

The guarded library surface also includes the generic Wealthfolio client's
account and activity methods; Vanguard history execution; rebuild account,
asset, and decision application; and every write capability in
`SpendingAdapter`. Those spending capabilities cover assignment, category
create/update/move/delete, rule create/update/delete/rerun, and budget target,
rollover, group, group-assignment, and copy operations. Unknown non-`GET`
Wealthfolio routes fail closed, so a newly added write does not become usable
merely because it was omitted from this inventory.

## Verify containment

Run the focused synthetic tests with the opt-in absent:

```sh
python -m pytest tests/test_mutation_interlock.py -q
```

They prove default and invalid-value denial, exact-value acceptance, operation
identification, coverage across mutation families, and that transport is never
called before denial. Plan, dry-run, diagnostics, raw SimpleFIN
`pull-plan`, known read-only `POST` endpoints, and
`POST /utilities/database/backup` remain usable without opt-in.

## Reviewed mutation runs

Enable the interlock only for the lifetime of one reviewed command. For example,
in PowerShell:

```powershell
$env:WEALTHFOLIO_MUTATIONS_ENABLED = "true"
try {
    python -m importers.rebuild.current_cli quotes <reviewed arguments> --apply
} finally {
    Remove-Item Env:WEALTHFOLIO_MUTATIONS_ENABLED
}
```

Or in a POSIX shell:

```sh
WEALTHFOLIO_MUTATIONS_ENABLED=true \
  python -m importers.rebuild.current_cli quotes <reviewed arguments> --apply
```

Opening the global interlock does **not** bypass plan fingerprints, environment
identity checks, staging-only restrictions, production-port restrictions,
`--allow-production`, receipts, evidence hashes, or any other command-specific
safety check. Every applicable check must still pass independently.
