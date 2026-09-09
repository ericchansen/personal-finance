"""Import a Monarch Money export into Wealthfolio.

    python cli.py plan  --data-dir D:/documents/finance-data
    python cli.py apply --data-dir D:/documents/finance-data

`plan` is read-only: it parses the export, classifies accounts and writes a
plan file for review. `apply` creates the accounts and loads the activities.

Both are safe to re-run. Every activity carries a deterministic idempotency key
derived from the Monarch row id, and Wealthfolio rejects duplicates server-side,
so a repeated import adds nothing.

Account names are personal data. They are written to the plan and mapping files
in the external data directory, never into this repository.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import monarch  # noqa: E402
from wealthfolio_client import WealthfolioClient, WealthfolioError  # noqa: E402

BATCH_SIZE = 200
KEY_PREFIX = "monarch"
# Holdings are derived from the imported transaction history; an account left
# on NOT_SET is reported by the app as needing setup.
TRACKING_MODE = "TRANSACTIONS"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def mask(name: str) -> str:
    """Blunt digit masking so console output is safe to paste into a ticket."""
    return re.sub(r"\d{2,}", "****", name or "")


def newest(paths: list[Path]) -> Path:
    if not paths:
        raise SystemExit("no matching export file found")
    return max(paths, key=lambda p: p.stat().st_mtime)


def locate_exports(legacy_dir: Path) -> tuple[Path, Path]:
    txn = newest(sorted(legacy_dir.glob("Transactions*.csv")))
    bal = newest(sorted(legacy_dir.glob("Balances*.csv")))
    return txn, bal


def load_overrides(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def read_password(data_dir: Path) -> str:
    """Resolve the Wealthfolio password.

    Order: WEALTHFOLIO_PASSWORD, then the generated password file if it still
    exists, then an interactive prompt. The file is meant to be deleted once
    the password is in a password manager, so the env var is the supported way
    to run this unattended.
    """
    from_env = os.environ.get("WEALTHFOLIO_PASSWORD")
    if from_env:
        return from_env

    pw_file = data_dir / "wealthfolio" / "ADMIN-PASSWORD.txt"
    if pw_file.exists():
        for line in pw_file.read_text(encoding="utf-8").splitlines():
            candidate = line.strip()
            if candidate and " " not in candidate and not candidate.startswith(
                ("Wealthfolio", "Move", "Only", "generated")
            ):
                return candidate
    return getpass.getpass("Wealthfolio password: ")


def activity_payload(txn: monarch.Transaction, account_id: str, wf_account_type: str) -> dict:
    """Map one Monarch transaction onto a Wealthfolio activity.

    Amounts are sent as positive magnitudes; direction is carried by the
    activity type, which is how Wealthfolio models cash movement. The type is
    resolved against the destination account type because credit cards reject
    several otherwise-valid types.
    """
    return {
        "accountId": account_id,
        "activityType": monarch.resolve_activity_type(txn, wf_account_type),
        "activityDate": f"{txn.date.isoformat()}T00:00:00Z",
        "amount": abs(float(txn.amount)),
        "currency": "USD",
        "isDraft": False,
        "comment": txn.merchant or None,
        "idempotencyKey": f"{KEY_PREFIX}:{txn.source_id}" if txn.source_id else None,
    }


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------

def build_plan(data_dir: Path) -> dict:
    legacy = data_dir / "legacy" / "monarch"
    txn_path, bal_path = locate_exports(legacy)

    transactions = monarch.read_transactions(txn_path)
    balances = monarch.read_balances(bal_path)
    overrides = load_overrides(legacy / "account-overrides.json")
    profiles = monarch.build_profiles(transactions, balances, overrides=overrides)
    kept = monarch.trusted_balances(balances, profiles)
    cutoff = monarch.infer_data_cutoff(profiles)

    accounts = []
    for profile in sorted(profiles.values(), key=lambda p: -p.txn_count):
        accounts.append(
            {
                "name": profile.name,
                "inferredType": profile.account_type,
                "wealthfolioType": monarch.wealthfolio_account_type(profile),
                "transactions": profile.txn_count,
                "firstTxn": profile.first_txn.isoformat() if profile.first_txn else None,
                "lastTxn": profile.last_txn.isoformat() if profile.last_txn else None,
                "trustCutoff": profile.trust_cutoff.isoformat() if profile.trust_cutoff else None,
                "staleBalanceRows": profile.stale_balance_rows,
                "isClosed": profile.is_closed,
                "needsReview": profile.needs_review,
                "typeOverridden": profile.type_overridden,
            }
        )

    return {
        "source": {"transactions": txn_path.name, "balances": bal_path.name},
        "dataCutoff": cutoff.isoformat() if cutoff else None,
        "totals": {
            "transactions": len(transactions),
            "balancePoints": len(balances),
            "balancePointsTrusted": len(kept),
            "balancePointsDropped": len(balances) - len(kept),
            "accounts": len(profiles),
            "categories": len(monarch.category_summary(transactions)),
        },
        "accounts": accounts,
    }


def cmd_plan(args) -> int:
    data_dir = Path(args.data_dir)
    plan = build_plan(data_dir)
    out = data_dir / "normalized" / "monarch-plan.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(plan, indent=2), encoding="utf-8")

    totals = plan["totals"]
    print(f"source        : {plan['source']['transactions']}")
    print(f"data cutoff   : {plan['dataCutoff']}  (last real transaction)")
    print(f"transactions  : {totals['transactions']}")
    print(
        f"balances      : {totals['balancePointsTrusted']} trusted, "
        f"{totals['balancePointsDropped']} stale dropped"
    )
    print(f"accounts      : {totals['accounts']}  categories: {totals['categories']}")

    importable = [a for a in plan["accounts"] if a["wealthfolioType"] and not a["isClosed"]]
    closed = [a for a in plan["accounts"] if a["isClosed"]]
    skipped = [a for a in plan["accounts"] if not a["wealthfolioType"]]
    review = [a for a in plan["accounts"] if a["needsReview"]]

    print(f"\nwill import   : {len(importable)} accounts")
    for a in importable:
        print(f"  {mask(a['name'])[:38]:<40} {a['wealthfolioType']:<14} {a['transactions']:>6} txns")

    if closed:
        print(f"\nclosed (import as inactive): {len(closed)}")
        for a in closed:
            print(f"  {mask(a['name'])[:38]:<40} last txn {a['lastTxn']}")

    if skipped:
        print(f"\nnot accounts in Wealthfolio (need alternative-asset/liability handling): {len(skipped)}")
        for a in skipped:
            print(f"  {mask(a['name'])[:38]:<40} {a['inferredType']}")

    if review:
        print(f"\nNEEDS REVIEW - balance-only and stale; confirm open or closed: {len(review)}")
        for a in review:
            print(f"  {mask(a['name'])[:38]:<40} last real {a['trustCutoff']}")

    print(f"\nplan written to {out}")
    return 0


# --------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------

def import_batch(
    client: WealthfolioClient, batch: list[dict]
) -> tuple[int, int, list[str]]:
    """Send one batch, falling back to per-item on duplicate rejection.

    Returns ``(created, skipped, error_messages)``. Server-side validation
    errors are returned in the response body rather than raised, so they must
    be read explicitly; ignoring them silently discards rows.
    """
    def split(result: dict) -> tuple[int, list[str]]:
        errors = [e.get("message", "unknown") for e in (result.get("errors") or [])]
        return len(result.get("created", [])), errors

    try:
        created, errors = split(client.save_activities(creates=batch))
        return created, 0, errors
    except WealthfolioError as exc:
        if "uplicate" not in exc.body:
            raise

    created = skipped = 0
    errors: list[str] = []
    for item in batch:
        try:
            made, errs = split(client.save_activities(creates=[item]))
            created += made
            errors.extend(errs)
        except WealthfolioError as exc:
            if "uplicate" in exc.body:
                skipped += 1
            else:
                raise
    return created, skipped, errors


def cmd_apply(args) -> int:
    data_dir = Path(args.data_dir)
    legacy = data_dir / "legacy" / "monarch"
    txn_path, bal_path = locate_exports(legacy)

    transactions = monarch.read_transactions(txn_path)
    balances = monarch.read_balances(bal_path)
    overrides = load_overrides(legacy / "account-overrides.json")
    profiles = monarch.build_profiles(transactions, balances, overrides=overrides)

    client = WealthfolioClient(args.base_url, writer_data_dir=args.data_dir)
    if not client.health():
        raise SystemExit(f"Wealthfolio is not reachable at {args.base_url}")
    client.login(read_password(data_dir))

    print("backing up before import...")
    client.backup_database()

    existing = {a["name"]: a for a in client.list_accounts()}
    mapping_file = data_dir / "normalized" / "monarch-account-map.json"
    mapping: dict[str, str] = {}
    if mapping_file.exists():
        mapping = json.loads(mapping_file.read_text(encoding="utf-8"))

    account_types: dict[str, str] = {}
    created_accounts = reconciled_accounts = 0
    for profile in sorted(profiles.values(), key=lambda p: -p.txn_count):
        wf_type = monarch.wealthfolio_account_type(profile)
        if wf_type is None:
            continue
        account_types[profile.name] = wf_type
        group = monarch.default_group(profile)
        is_active = not profile.is_closed

        current = existing.get(profile.name)
        if current:
            mapping[profile.name] = current["id"]
            # Reconcile drift. An account left with the wrong type is not
            # cosmetic: only CASH and CREDIT_CARD feed spending reports, so a
            # retirement account mistyped as CASH turns its opening balance
            # into reported income.
            if (
                current.get("accountType") != wf_type
                or current.get("group") != group
                or bool(current.get("isActive")) != is_active
                or current.get("trackingMode") != TRACKING_MODE
            ):
                client.update_account(
                    current["id"],
                    name=profile.name,
                    accountType=wf_type,
                    currency=current.get("currency", "USD"),
                    isActive=is_active,
                    isDefault=current.get("isDefault", False),
                    group=group,
                    trackingMode=TRACKING_MODE,
                )
                reconciled_accounts += 1
            continue

        account = client.create_account(
            name=profile.name,
            account_type=wf_type,
            group=group,
            is_active=is_active,
        )
        mapping[profile.name] = account["id"]
        created_accounts += 1

    mapping_file.parent.mkdir(parents=True, exist_ok=True)
    mapping_file.write_text(json.dumps(mapping, indent=2), encoding="utf-8")
    print(
        f"accounts: {created_accounts} created, {reconciled_accounts} reconciled, "
        f"{len(mapping)} mapped"
    )

    payloads = [
        activity_payload(txn, mapping[txn.account], account_types[txn.account])
        for txn in transactions
        if txn.account in mapping
    ]
    orphaned = len(transactions) - len(payloads)
    if orphaned:
        print(f"note: {orphaned} transactions belong to non-account types and were skipped")

    total_created = total_skipped = 0
    failures: list[str] = []
    for start in range(0, len(payloads), BATCH_SIZE):
        batch = payloads[start : start + BATCH_SIZE]
        created, skipped, errors = import_batch(client, batch)
        total_created += created
        total_skipped += skipped
        failures.extend(errors)
        done = min(start + BATCH_SIZE, len(payloads))
        print(f"  {done}/{len(payloads)}  created={total_created} skipped={total_skipped}", end="\r")

    print(f"\nactivities: {total_created} created, {total_skipped} already present")

    if failures:
        # Rejections used to pass unnoticed because only the created count was
        # read, which silently dropped most of an import.
        print(f"\n{len(failures)} REJECTED. Distinct reasons:")
        for reason, count in Counter(failures).most_common(10):
            print(f"  {count:>6}  {reason[:110]}")
        return 1

    expected = len(payloads)
    if total_created + total_skipped != expected:
        print(f"\nWARNING: expected {expected}, accounted for {total_created + total_skipped}")
        return 1
    return 0


# --------------------------------------------------------------------------

def fetch_activity_ids(client: WealthfolioClient, page_size: int = 1000) -> dict[str, str]:
    """Map every idempotency key to its Wealthfolio activity id."""
    mapping: dict[str, str] = {}
    for row in client.iter_activities(page_size=page_size):
        key = row.get("idempotencyKey")
        if key:
            mapping[key] = row["id"]
    return mapping


def cmd_link(args) -> int:
    """Link the two legs of each internal transfer.

    Without this, one leg counts as spending and the other as income, which
    inflates both sides of every cash-flow report.
    """
    data_dir = Path(args.data_dir)
    legacy = data_dir / "legacy" / "monarch"
    txn_path, _ = locate_exports(legacy)
    transactions = monarch.read_transactions(txn_path)

    pairs = monarch.find_transfer_pairs(transactions, max_days=args.max_days)
    print(f"matched {len(pairs)} transfer pairs from {len(transactions)} transactions")
    if args.dry_run:
        print("dry run; nothing sent")
        return 0

    client = WealthfolioClient(args.base_url, writer_data_dir=args.data_dir)
    if not client.health():
        raise SystemExit(f"Wealthfolio is not reachable at {args.base_url}")
    client.login(read_password(data_dir))

    print("resolving activity ids...")
    ids = fetch_activity_ids(client)
    print(f"  {len(ids)} activities indexed")

    linked = missing = failed = already = 0
    reasons: list[str] = []
    for index, (out, inn) in enumerate(pairs, 1):
        a = ids.get(f"{KEY_PREFIX}:{out.source_id}")
        b = ids.get(f"{KEY_PREFIX}:{inn.source_id}")
        if not a or not b:
            missing += 1
            continue
        try:
            client.post("/activities/link", {"activityAId": a, "activityBId": b})
            linked += 1
        except WealthfolioError as exc:
            # Re-running is expected, so an existing link is not a failure.
            if "already linked" in exc.body:
                already += 1
            else:
                failed += 1
                reasons.append(exc.body[:120])
        if index % 100 == 0:
            print(f"  {index}/{len(pairs)} linked={linked} already={already}", end="\r")

    print(f"\nlinked {linked}, already linked {already}, unresolved {missing}, failed {failed}")
    if reasons:
        for reason, count in Counter(reasons).most_common(5):
            print(f"  {count:>5}  {reason}")
    return 0


# --------------------------------------------------------------------------

def cmd_balances(args) -> int:
    """Reconcile each account to its last trusted balance.

    Importing transactions alone leaves every account short by whatever it held
    on day one, which is why the dashboard reads negative.
    """
    data_dir = Path(args.data_dir)
    legacy = data_dir / "legacy" / "monarch"
    txn_path, bal_path = locate_exports(legacy)

    transactions = monarch.read_transactions(txn_path)
    balances = monarch.read_balances(bal_path)
    overrides = load_overrides(legacy / "account-overrides.json")
    profiles = monarch.build_profiles(transactions, balances, overrides=overrides)
    openings = monarch.compute_opening_balances(transactions, balances, profiles)

    print(f"{'ACCOUNT':<34} {'TARGET':>13} {'FROM TXNS':>13} {'OPENING':>13}  AS OF")
    print("-" * 92)
    for opening in openings:
        print(
            f"{mask(opening.account)[:32]:<34} {opening.target_balance:>13,.2f} "
            f"{opening.activity_sum:>13,.2f} {opening.amount:>13,.2f}  {opening.as_of}"
        )
    print(f"\n{len(openings)} accounts need an opening balance")

    if args.dry_run:
        print("dry run; nothing sent")
        return 0

    client = WealthfolioClient(args.base_url, writer_data_dir=args.data_dir)
    if not client.health():
        raise SystemExit(f"Wealthfolio is not reachable at {args.base_url}")
    client.login(read_password(data_dir))
    client.backup_database()

    mapping_file = data_dir / "normalized" / "monarch-account-map.json"
    if not mapping_file.exists():
        raise SystemExit("no account map; run `apply` first")
    mapping = json.loads(mapping_file.read_text(encoding="utf-8"))
    account_types = {
        name: monarch.wealthfolio_account_type(profile)
        for name, profile in profiles.items()
    }

    payloads = []
    for opening in openings:
        account_id = mapping.get(opening.account)
        wf_type = account_types.get(opening.account)
        if not account_id or not wf_type:
            continue
        payloads.append(
            {
                "accountId": account_id,
                "activityType": monarch.opening_activity_type(opening, wf_type),
                "activityDate": f"{opening.as_of.isoformat()}T00:00:00Z",
                "amount": abs(float(opening.amount)),
                "currency": "USD",
                "isDraft": False,
                "comment": "Opening balance (imported from Monarch)",
                # Stable so re-running never double-applies.
                "idempotencyKey": f"{KEY_PREFIX}:opening:{account_id}",
            }
        )

    created, skipped, errors = import_batch(client, payloads)
    print(f"opening balances: {created} created, {skipped} already present")
    if errors:
        for reason, count in Counter(errors).most_common(5):
            print(f"  {count:>4}  {reason[:110]}")
        return 1
    return 0


# --------------------------------------------------------------------------

def cmd_external(args) -> int:
    """Mark transfers that cross the tracked-account boundary as external.

    Wealthfolio flags an unpaired transfer as an incomplete transfer and warns
    that the flow "may distort returns". Most of these are genuine boundary
    crossings — paying a person over Venmo, or moving money to an institution
    that is not in the export — so the honest resolution is to declare them
    external rather than to keep widening the matcher until unrelated amounts
    pair by coincidence.
    """
    data_dir = Path(args.data_dir)
    legacy = data_dir / "legacy" / "monarch"
    txn_path, _ = locate_exports(legacy)
    transactions = monarch.read_transactions(txn_path)

    pairs = monarch.find_transfer_pairs(transactions, max_days=args.max_days)
    unpaired = monarch.find_unpaired_transfers(transactions, max_days=args.max_days)
    paired_keys = {f"{KEY_PREFIX}:{t.source_id}" for pair in pairs for t in pair}
    unpaired_keys = {f"{KEY_PREFIX}:{t.source_id}" for t in unpaired if t.source_id}

    print(f"transfers: {len(pairs) * 2} paired, {len(unpaired)} unpaired")
    by_account = Counter(t.account for t in unpaired)
    for name, count in by_account.most_common(8):
        print(f"   {mask(name)[:36]:<38} {count:>5}")
    if args.dry_run:
        print("dry run; nothing sent")
        return 0

    client = WealthfolioClient(args.base_url, writer_data_dir=args.data_dir)
    if not client.health():
        raise SystemExit(f"Wealthfolio is not reachable at {args.base_url}")
    client.login(read_password(data_dir))
    client.backup_database()

    print("scanning transfer activities...")
    rows = list(
        client.iter_activities(activity_types=["TRANSFER_IN", "TRANSFER_OUT"])
    )
    print(f"  {len(rows)} transfer activities")

    external = json.dumps({"flow": {"is_external": True}})
    internal = json.dumps({"flow": {"is_external": False}})

    updated = skipped = 0
    for row in rows:
        key = row.get("idempotencyKey")
        if key in unpaired_keys:
            want, flag = external, True
        elif key in paired_keys:
            # Linking already sets this; re-assert so a stray edit cannot leave
            # a linked leg marked external.
            want, flag = internal, False
        else:
            skipped += 1
            continue

        current = row.get("metadata")
        if isinstance(current, dict) and current.get("flow", {}).get("is_external") is flag:
            skipped += 1
            continue

        client.put(
            "/activities",
            {
                "id": row["id"],
                "accountId": row["accountId"],
                "activityType": row["activityType"],
                "activityDate": row["date"],
                "currency": row["currency"],
                "amount": row["amount"],
                "isDraft": False,
                "metadata": want,
            },
        )
        updated += 1
        if updated % 100 == 0:
            print(f"  updated {updated}", end="\r")

    print(f"\nmarked {updated} activities, {skipped} already correct")
    return 0


# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["plan", "apply", "link", "balances", "external"]
    )
    parser.add_argument("--data-dir", default="D:/documents/finance-data")
    parser.add_argument("--base-url", default="http://127.0.0.1:8088")
    parser.add_argument("--max-days", type=int, default=5,
                        help="how far apart the two legs of a transfer may post")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    commands = {
        "plan": cmd_plan, "apply": cmd_apply, "link": cmd_link,
        "balances": cmd_balances, "external": cmd_external,
    }
    return commands[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
