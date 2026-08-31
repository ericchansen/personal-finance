# Citi — transaction export

Derived from downloaded files rather than a recorded session. The Citi
recording was lost to a recorder bug (output was buffered and written at exit;
a hard kill discarded it), and the recorder now streams to disk instead. The
endpoint is therefore undocumented here — only the manual procedure and the
file formats, both verified against real exports.

**Automation tier: unknown** — not yet observed. Re-record a session to fill
this in.

## Manual procedure

1. Sign in at `citi.com`
2. Open an account → Activity → Download / Export
3. Choose a format (see below) and a date range
4. Repeat per account

Roughly **18 months** of history is available. That does not divide neatly into
calendar years, so exports end up as a year plus a stub — for example a 2025
file and a 2026-01-01-to-date file. Overlapping ranges are safe: OFX dedupes on
`FITID`, and the CSV loader dedupes on a synthesized key.

## Prefer OFX over CSV

Citi offers both for the same data. **OFX is strictly better** and should be
taken whenever offered:

|  | OFX | CSV |
|---|---|---|
| Transaction id | `FITID`, stable | none |
| Account id | `ACCTID` (masked) | none |
| Balance | `BALAMT` + `DTASOF` | none |
| Account type | `ACCTTYPE` | none |

Without a transaction id, imports must dedupe on a synthesized key of date,
amount and description. Two identical same-day purchases from the same merchant
then collapse into one. That under-counts, which is the safer failure, but it is
still a real loss of fidelity that OFX avoids entirely.

OFX also identifies its own account, so a mis-filed download can be recovered.
A Citi CSV cannot.

## OFX shape

```
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20260819120000
<TRNAMT>-45.20
<FITID>...
<NAME>MERCHANT NAME
</STMTTRN>
```

OFX is **SGML, not XML**: closing tags are optional and usually absent, so a
tree parser fails on it. Values must be read up to the next tag or newline.

`ACCTID` is masked, and **the masking style differs between Citi's own
products**: cards may use `XXXXXXXXXXXX1234` (twelve X's), while savings may use `*****5678`
(five asterisks). Both preserve the last four, which is enough to tell accounts
apart but not to reconstruct a number. Match on the last four, not on the whole
string.

⚠️ **`DTEND` is not trustworthy.** An export can carry an end date before its
start date. Take the range from the transactions themselves, not the header.

## CSV shape

```csv
Status,Date,Description,Debit,Credit
```

Three traps, all found by diffing the CSV against the OFX for the same account.

### 1. The date format differs between Citi's own products

| Product | Format | Example |
|---|---|---|
| Credit card | `MM/DD/YYYY` | `12/31/2025` |
| Savings | `MM-DD-YYYY` | `07-24-2026` |

Same bank, same export screen, same header row. A parser that assumes one
format silently drops every row of the other.

### 2. Signs are inconsistent, even between Citi's own products

On a card, a charge is a positive `Debit` and a payment a **negative** `Credit`.
On a savings account, interest arrives as a **positive** `Credit`. Negating both
columns reconciles the cards and inverts savings.

What holds everywhere is the *meaning* of the columns: `Debit` is money out,
`Credit` is money in. So negate `Debit` — which also turns a negative debit, a
refund, back into an inflow — and treat `Credit` as an inflow regardless of the
sign the file happens to use.

Synthetic debit, refund, payment, and interest fixtures verify these sign rules
against equivalent OFX rows.

### 3. A zero is a real transaction

A waived $0.00 membership fee arrives as `Debit=0.00` with an empty `Credit`.
Treating zero as "absent" and falling through to the Credit column drops the row
entirely. **The column that is present decides, not the column that is
non-zero.**

Savings rows also carry a trailing comma, producing an extra empty field.

When both formats cover the same account and date range, compare their
transaction counts and totals before accepting an import.

## What to do next

Re-record a Citi session with `recorder/record.mjs` to capture the export
endpoint, as was done for Ally and Fidelity. Citi's dashboard is a React app
behind Akamai, so the recorder's clean-profile approach matters: an automated
browser is blocked where a plain debug-port session is not.

Citi's terms prohibit automated access. This is documented to make a manual
export faster and reproducible, not to run unattended against their servers.
