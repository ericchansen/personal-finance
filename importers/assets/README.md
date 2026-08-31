# Property, vehicle and loan importer

Creates alternative assets and liabilities in Wealthfolio.

```sh
python cli.py --data-dir D:/documents/finance-data --dry-run
python cli.py --data-dir D:/documents/finance-data
```

## Why these are not accounts

Alternative assets sit outside the account/activity model — a record plus a
valuation. A house therefore needs no fake account and no stream of invented
transactions, and a mortgage does not have to be modelled as a credit card,
which is the only account type Wealthfolio treats as a liability.

## Loan balances are derived, not entered

A statement is a snapshot that goes stale the moment it is filed. Instead the
balance is amortized from the loan's origination terms up to a given date, so
the figure is current whenever the importer runs and needs no fresh document.

Early loan payments can be dominated by interest, so amortization is necessary
to derive a defensible current principal balance.

Pass `--as-of` to value the loan at a different date.

## Configuration

Real values are personal data, so configuration lives in the external data
directory at `<data>/assets/holdings.json`, never in this repository. Copy
`holdings.example.json` as a starting point.

A loan entry needs only `principal`, `annualRate`, `termMonths` and
`firstPayment`; the balance follows. `linkedTo` names an asset in the same file
so the app can group a mortgage with the property it finances.

## API notes

- **Asset kinds are lowercase** in the API (`property`, `vehicle`,
  `collectible`, `precious`, `liability`, `other`) even though the frontend
  constants are SCREAMING_CASE. Sending `PROPERTY` returns a 422. The importer
  accepts either and normalizes.
- Existing assets are matched by name, so re-running does not duplicate them.

### ⚠️ A liability must share its asset's valuation date

An alternative holding is a **single point-in-time valuation**, and it enters
the net worth history on that date — not before it.

So a house dated at its appraisal and its mortgage dated today leave a window
where the asset is counted and the debt is not. This can materially overstate
historical net worth while leaving today's figure correct.

A loan linked to an asset therefore inherits that asset's valuation date, and
is amortized to that date rather than to today so its balance and its date
describe the same moment. An explicit `valueDate` on the loan overrides this.

### ⚠️ `POST /alternative-assets` does not upsert

There is no `PUT`. Posting again with an existing `id` **ignores the id and
creates a second asset**, and both are then counted — which silently doubles a
liability. Check `/alternative-holdings` for an existing name before posting;
the importer does.

## Known gaps

- Property values are entered manually and do not track a market estimate.
- Only the current valuation is recorded, so a property contributes a flat line
  to the historical net-worth chart rather than an appreciation curve, and a
  mortgage a flat line rather than an amortization curve. Because the loan is
  amortized to its valuation date, the balance is right on that date and
  increasingly stale after it — the opposite trade from dating it today, and
  the better one, since a wrong *date* distorts months of history while a
  slightly stale balance is off by the principal paid since.
- Escrow, PMI and extra principal payments are not modelled; the balance
  reflects scheduled principal and interest only.
