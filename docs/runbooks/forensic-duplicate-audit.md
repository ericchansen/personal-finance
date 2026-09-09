# Forensic duplicate audit

Use this audit to quantify possible duplicate economic activity before planning
any remediation. It reads an already verified evidence baseline and sealed
external evidence only. It has no Wealthfolio client, HTTP transport, apply
command, or delete command.

All detailed output is private. Keep `--data-dir` outside the repository. Never
copy `private-audit.json`, either CSV, or a review-decision file into this public
repository.

## Evidence prerequisites

Create a fresh [evidence baseline](evidence-baseline.md) after sealing all
relevant evidence:

- SimpleFIN raw snapshots
- reviewed SimpleFIN plans
- staging or production application plans
- successful application receipts
- the source evidence and current authenticated ledger baseline

The audit verifies the baseline pointer, manifest, domain hashes, and
source-file hashes before reading activity data. The mutable canonical output
is a downstream publication, so it is verified separately when the audit is
built and is not treated as an upstream baseline input. Legacy baselines retain
its predecessor hash as sealed historical evidence without requiring the
downstream directory to remain unchanged. A matching-
environment SimpleFIN application plan is accepted only when its plan fingerprint
and every nested evidence binding agree with the baseline. A receipt is linked
only by the exact application-plan hash, plan and intent fingerprints,
environment and ledger fingerprints, operation counts, and reconciliation
records.

Only plans and receipts whose `environmentFingerprint` exactly matches the
verified baseline may supply canonical aliases, lineage, or reconciliation
proof. A sealed plan or receipt from another environment is counted as ignored
before nested evidence binding and cannot influence the candidate graph. Missing
or invalid environment fingerprints and invalid document seals fail closed.

If any input is missing, changed, unsealed, or ambiguous, the command fails
closed or records missing/ambiguous lineage. It never infers reconciliation
compensation from equal amounts.

Application reports named `apply-report-*.json` are classified as receipts.
The audit also detects validated plan and receipt document shapes across every
hashed JSON input, so older baselines that labeled such a report as
`other-evidence` still expose missing or proven receipt lineage.

## Build and verify

```powershell
python -m importers.audit.forensic_cli build --data-dir <finance-data>
python -m importers.audit.forensic_cli verify --data-dir <finance-data>
```

The console prints hashes and aggregate counts only. Publications are written
under:

```text
<finance-data>/audit/duplicates/
  current.json
  publications/<manifest-sha256>/
    manifest.json
    private-audit.json
    activities.csv
    candidates.csv
    review-decisions.json
    summary.json
    summary.md
```

`current.json` is atomically replaced only after the content-addressed
publication and all input hashes verify. Rebuilding unchanged inputs reuses the
same publication ID. A changed existing publication is rejected as corruption.

`summary.json` and `summary.md` are privacy-preserving: they contain hashes,
counts, fixed reason codes, fixed source-family pairs, and ambiguity
cardinalities. They omit amounts, descriptions, account and transaction
identities, source paths, notes, metadata, and dependent-state values.

## Classification rules

The forensic audit remains conservative: only exact, provider-scoped source
identity within one canonical account is classified automatically. Duplicate
candidates remain `review-required`, including:

- exact account/date/amount/description matches across source families
- bounded-date equal-amount matches
- provider-shortened, prefix, or token-similar descriptions
- one-to-many and many-to-many candidate graphs
- same-connection cross-account mirrors
- closed or reissued account overlaps

Linked and inferred opposite-sign, cross-account transfer pairs are classified
as `relationship-only`. They remain visible in the graph and aggregate counts,
but are excluded from duplicate remediation queues. A conflicting source group
that does not contain opposite-sign legs on different accounts remains
`review-required`.

Same-source records with different source identities are not treated as
duplicates merely because their date, amount, and description repeat.
Currencies are matched independently. UTC normalization makes midnight and noon
display anchors comparable by source date without changing their raw private
timestamps.

Investment `BUY` and `SELL` activities remain fully accounted for but are
explicitly out of scope for cash duplicate matching. Their quantity or amount is
never interpreted as a signed cash effect. Private and shareable outputs report
the fixed `non-cash-investment-activity` reason and aggregate count.

## Review dependent state first

For every baseline activity, `private-audit.json` records whether it has:

- a transfer counterpart
- category assignments
- splits
- spending-event links
- user notes or metadata
- projection or import run IDs

These records identify state that a future delete could cascade or lose. The
audit fails closed if any required dependent-state baseline domain is
unavailable or incompatible; it never reports an unknown domain as empty. The
`comment` field is treated as the provider description, never as a proven user
note. Only distinct note fields or metadata establish additional user-authored
state. The current Wealthfolio API does not expose an authenticated
delete-cascade preview, note author/edit history, or complete projection
provenance, so those gaps stay explicit in the private publication.

Duplicate economic effects and receipt-proven reconciliation effects are
separate lists. Unresolved candidate effects are listed individually with
`netted: false`; the audit never nets ambiguity away.

## Record review decisions

`review-decisions.json` contains deterministic pending templates identified
only by candidate hashes. Treat the publication copy as immutable. Build the
versioned [private lineage review](lineage-review.md) queue to create
risk-ordered packets, import fully bound reviewer decisions, and calculate
remediation readiness.

The forensic workflow deliberately ends at evidence publication. Neither it
nor the lineage-review workflow has an apply or delete command.
Any future remediation requires a separate design, fresh baseline, backup,
cascade analysis, and independently reviewed mutation controls.
