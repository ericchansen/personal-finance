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

Citi offers both for the same data. **Prefer OFX for stable transaction identity,
and keep the matching CSV when available**: it can retain description text that
the OFX export truncates.

|  | OFX | CSV |
|---|---|---|
| Transaction id | `FITID`, stable | none |
| Account id | `ACCTID` (masked) | none |
| Balance | `BALAMT` + `DTASOF` | none |
| Account type | `ACCTTYPE` | none |

Without a transaction id, imports use a synthesized key plus an occurrence ordinal
to preserve identical rows within an export. Cross-export identity remains less
reliable than a stable `FITID`; do not treat identical descriptions as unique IDs.

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

⚠️ **Validate the statement window.** An export can carry an end date before its
start date. An invalid header cannot establish complete coverage. The minimum and
maximum transaction dates describe observed rows, not the missing coverage claim.

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

## Source-bound description correspondence

The shared canonical/shadow extract reader supports an explicit
`descriptionEvidence` declaration on an existing OFX mapping entry:

```json
{
  "rule": "citi-ofx-name-27-v2",
  "path": "synthetic/paired-export.csv",
  "primarySha256": "<SHA-256 of the exact OFX bytes>",
  "supportingSha256": "<SHA-256 of the exact CSV bytes>"
}
```

The declaration scopes both exports to the existing mapped account; a neighboring
filename is never discovered or trusted automatically. Both byte hashes, strict
row inventories, and the complete date/signed-amount multiset must agree. The
reader expands only a unique date/amount occurrence whose OFX name equals the
first 27 characters of the CSV description (ignoring trailing padding). It keeps
the original `FITID`, date, amount, and raw file unchanged. Repeated or mismatched
buckets remain unexpanded rather than guessing correspondence.

Version 2 also separates the issuer's exact terminal
`null XXXXXXXXXXXX1234` shape into `paymentInstrumentMask` evidence instead of
treating it as part of the merchant description. The full CSV description and
original OFX name remain in evidence. Other suffixes, references, and unmasked
numbers are not removed. The mask is a payment-instrument hint, not a new account
mapping or proof of the cardholder's identity. This makes a full descriptor
comparison possible without using a shared merchant phone number as a
transaction reference. Version 1 declarations retain their original behavior.

The current rule requires one primary account, an explicit USD `CURDEF`, posted/cleared
CSV rows, and unambiguous debit/credit columns. It does not infer a currency or
silently omit malformed or pending rows to make the files match.

Both artifacts enter the source manifest. The shadow records original and expanded
descriptions with the rule and source hashes. The CSV is supporting evidence, not
another imported transaction set. This source-side fixed-width restoration is
different from matching an aggregator by a generic merchant prefix or phone number.
It does not, by itself, certify every cross-provider duplicate.

When mapped OFX files overlap, the shared reader applies the same verified
enrichment to exact replays of the complete raw occurrence in the same mapped and
source account. This happens before canonical deduplication; enriching only one
copy must not manufacture two conflicting identities from one `FITID`. Changed
raw content and different accounts do not inherit the evidence. Contradictory
supporting descriptions fail explicitly, and a supporting CSV cannot also be
imported as a second transaction set.

## What to do next

Re-record a Citi session with `recorder/record.mjs` to capture the export
endpoint, as was done for Ally and Fidelity. Citi's dashboard is a React app
behind Akamai, so the recorder's clean-profile approach matters: an automated
browser is blocked where a plain debug-port session is not.

Citi's terms prohibit automated access. This is documented to make a manual
export faster and reproducible, not to run unattended against their servers.
