# Current property and vehicle valuations

Use this workflow when Wealthfolio asks for an updated property or vehicle value.
The workflow never scrapes a site, estimates depreciation, or writes to Wealthfolio.
Real addresses, VINs, mileage, values, offers, screenshots, and reports belong only
under `D:\documents\finance-data`.

## Obtain vehicle evidence

1. Record the odometer reading locally.
2. Request a dated offer or estimate directly from the manufacturer, a local dealer,
   or another buyer. Tesla documents its trade-in process at
   [Tesla Support](https://www.tesla.com/support/trade-ins). It may require the VIN,
   mileage, photos, and vehicle documents, so enter those only on the provider's site.
3. Save the returned PDF or screenshot under
   `raw/valuations/<YYYY-MM>/` in the private data directory. Do not use an emailed
   marketing range or the original purchase price as a current value. Mark the
   completed source artifact read-only.
4. If privacy is more important than convenience, ask a local dealer for a written
   appraisal without authorizing marketing contact or account linking.

## Obtain property evidence

1. For the strongest private evidence, commission a state-licensed appraiser and save
   the dated report locally. The
   [Appraisal Subcommittee registry](https://www.asc.gov/national-registries)
   provides the official route to verify an appraiser's credential.
2. A broker's dated comparative market analysis is a practical lower-cost alternative.
   Save the actual analysis, not a verbal estimate.
3. An automated valuation model can be supporting evidence only after the owner reviews
   the address, property characteristics, and valuation date. Federal regulators require
   quality controls when mortgage originators use AVMs; see the
   [CFPB final rule](https://www.consumerfinance.gov/rules-policy/final-rules/quality-control-standards-for-automated-valuation-models/).
   A consumer-facing estimate is still not an appraisal.
4. A county assessment is authoritative for tax assessment, not necessarily current
   market value. Label it accurately if used.

## Record explicit evidence

Copy `importers/valuations/evidence.example.json` to
`<data>\valuations\evidence\<descriptive-name>.json`. Set `entityId` to the exact
canonical fact ID, preserve the evidence date and amount exactly, and point
`sourceFile` to a distinct read-only artifact below `raw/valuations`. Evidence
JSON, fact files, and generated outputs cannot cite themselves as evidence.
Then run:

```powershell
python -m importers.valuations.cli --data-dir D:\documents\finance-data plan
```

The command writes an immutable, fingerprinted plan and canonical-schema monthly CSV
under the private data directory. A stale or missing property/vehicle source produces
`review-needed`; it is never carried forward as a current value. Loan rows come only
from uniquely mapped, current-month SimpleFIN monitor balances, and a positive
liability sign is blocked rather than guessed.

## Compare with staging (read-only)

Create a staging Wealthfolio instance on a loopback port other than `8088`, using a
private copy of the database. Never point this command at production:

```powershell
python -m importers.valuations.cli --data-dir D:\documents\finance-data project `
  --refresh-plan <private-plan-path> `
  --base-url http://127.0.0.1:18088
```

Review exact identities, values, dates, net-worth deltas, evidence hashes, blockers,
and the staging environment fingerprint in the private comparison report. The currently
consumed [upstream Wealthfolio](https://github.com/wealthfolio/wealthfolio) API provides
neither atomic conditional quote creation nor a verifiable exclusive-write mechanism.
A separate read followed by a write therefore has an unavoidable race. Until upstream
provides one of those primitives, this workflow is plan-only: it has no apply option and
never writes holdings, quotes, or database backups. `--apply` is rejected on every port,
including staging port `18088`. Port `8088` remains rejected for comparison planning.
Projection files are created exclusively: identical reruns are idempotent, while
different bytes at the same path stop the workflow.
