"""Load Ledger holdings or enforce current canonical account assertions."""

from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path

from importers.monarch.wealthfolio_client import WealthfolioClient
from importers.facts.loader import load_facts
from importers.facts.schema import DecisionFact

from .accounts import build_aliases, load_canonical_accounts
from .current import assertion_payloads, ledger_payloads, quote_payloads, rollover_plan
from .decisions import DecisionError
from .safety import instance_fingerprint, plan_fingerprint, validate_apply_target


def matching_health_issues(parsed, issues):
    code = parsed.data.get("issueCode")
    affected_ids = set(parsed.fact.affects)
    matches = [issue for issue in issues if issue["code"] == code]
    if not affected_ids:
        return matches
    return [
        issue
        for issue in matches
        if affected_ids.intersection(
            item.get("id") for item in issue.get("affectedItems") or []
        )
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("ledger", "assertions", "quotes", "rollovers", "health")
    )
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--source-data-dir", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--plan-fingerprint")
    args = parser.parse_args(argv)
    password = os.environ.get("WEALTHFOLIO_PASSWORD")
    if not password:
        parser.error("WEALTHFOLIO_PASSWORD is required")
    client = WealthfolioClient(args.base_url)
    client.login(password)
    environment = instance_fingerprint(client, args.base_url)
    account_map = json.loads(
        (args.data_dir / "canonical-account-map.json").read_text(encoding="utf-8")
    )
    canonical = args.source_data_dir / "normalized" / "canonical"
    facts = load_facts(args.source_data_dir / "facts")
    if not facts.ok:
        parser.error(f"private facts are invalid: {facts.errors[0]}")
    if args.command == "ledger":
        payloads = ledger_payloads(canonical, account_map)
        existing_keys = {
            row.get("idempotencyKey") for row in client.iter_activities()
        }
        payloads = [
            payload
            for payload in payloads
            if payload.get("idempotencyKey") not in existing_keys
        ]
    elif args.command == "assertions":
        with (canonical / "accounts.csv").open(encoding="utf-8-sig", newline="") as source:
            account_rows = list(csv.DictReader(source))
        account_types = {row["account_id"]: row["kind"] for row in account_rows}
        closed_ids = {row["account_id"] for row in account_rows if row["closed"]}
        closed = {
            account_id
            for parsed in facts.facts
            if isinstance(parsed.fact, DecisionFact)
            and parsed.fact.kind == "duplicate-account"
            and not parsed.fact.resolution.upper().startswith("DEFERRED")
            for account_id in parsed.fact.affects
            if account_id in closed_ids
        }
        canonical_accounts = load_canonical_accounts(canonical / "accounts.csv")
        aliases = build_aliases(args.source_data_dir, canonical_accounts)
        fidelity = json.loads(
            (
                args.source_data_dir / "extracts" / "fidelity" / "mapping.json"
            ).read_text(encoding="utf-8")
        )
        fidelity_names = [
            spec["name"] for spec in fidelity.get("accounts", {}).values()
        ] + [fidelity.get("espp", {}).get("name", "")]
        fidelity_ids = {
            aliases[name.casefold()]
            for name in fidelity_names
            if name and name.casefold() in aliases
        }
        zero_ids = {
            aliases[fidelity["accounts"][number]["name"].casefold()]
            for number in fidelity.get("zeroOut", [])
            if number in fidelity.get("accounts", {})
            and fidelity["accounts"][number]["name"].casefold() in aliases
        }
        closed.update(zero_ids)
        vanguard = json.loads(
            (
                args.source_data_dir / "extracts" / "vanguard" / "mapping.json"
            ).read_text(encoding="utf-8")
        )
        vanguard_ids = {
            aliases[spec["name"].casefold()]
            for spec in vanguard.get("accounts", {}).values()
            if spec["name"].casefold() in aliases
        }
        skip = (fidelity_ids - zero_ids) | vanguard_ids
        payloads, differences = assertion_payloads(
            client,
            canonical,
            account_map,
            account_types,
            closed_accounts=closed,
            skip_accounts=skip,
        )
        print(f"assertions checked={len(differences)}")
    elif args.command == "quotes":
        payloads = quote_payloads(client, facts.facts)
        fingerprint = plan_fingerprint(payloads)
        print(
            f"quotes={len(payloads)} fingerprint={fingerprint} "
            f"environment={environment}"
        )
        if args.apply:
            try:
                validate_apply_target(
                    client, args.base_url, fingerprint, args.plan_fingerprint
                )
            except DecisionError as exc:
                parser.error(str(exc))
        if args.apply and payloads:
            client.post(
                "/market-data/quotes/import",
                {"quotes": payloads, "overwriteExisting": True},
            )
            client.post("/portfolio/recalculate", {})
        return 0
    elif args.command == "rollovers":
        creates, updates, links = rollover_plan(client, facts.facts, account_map)
        fingerprint = plan_fingerprint((creates, updates, links))
        print(
            f"create={len(creates)} update={len(updates)} link={len(links)} "
            f"fingerprint={fingerprint} environment={environment}"
        )
        if not args.apply:
            return 0
        try:
            validate_apply_target(
                client, args.base_url, fingerprint, args.plan_fingerprint
            )
        except DecisionError as exc:
            parser.error(str(exc))
        result = client.save_activities(creates=creates, updates=updates)
        errors = result.get("errors") or []
        if errors or len(result.get("created", [])) != len(creates):
            parser.error(f"rollover reconstruction failed: {errors[:1]}")
        created = {
            row.get("idempotencyKey"): row["id"] for row in result.get("created", [])
        }
        for existing_id, created_key in links:
            client.post(
                "/activities/link",
                {"activityAId": existing_id, "activityBId": created[created_key]},
            )
        client.post("/portfolio/recalculate", {})
        return 0
    else:
        status = client.get("/health/status")
        dismissals = [
            parsed
            for parsed in facts.facts
            if isinstance(parsed.fact, DecisionFact)
            and parsed.fact.kind == "health-dismissal"
        ]
        dismissed = 0
        matched_issues = []
        for parsed in dismissals:
            matches = matching_health_issues(parsed, status.get("issues", []))
            if not matches:
                continue
            if len(matches) != 1:
                parser.error(
                    f"health decision {parsed.fact.id} matched {len(matches)} issues"
                )
            issue = matches[0]
            matched_issues.append(
                {"issueId": issue["id"], "dataHash": issue["dataHash"]}
            )
            dismissed += 1
        fingerprint = plan_fingerprint(matched_issues)
        print(
            f"health-dismissals={dismissed} fingerprint={fingerprint} "
            f"environment={environment}"
        )
        if args.apply:
            try:
                validate_apply_target(
                    client, args.base_url, fingerprint, args.plan_fingerprint
                )
            except DecisionError as exc:
                parser.error(str(exc))
            for payload in matched_issues:
                client.post("/health/dismiss", payload)
        return 0
    fingerprint = plan_fingerprint(payloads)
    print(
        f"create={len(payloads)} fingerprint={fingerprint} "
        f"environment={environment}"
    )
    if not args.apply:
        return 0
    try:
        validate_apply_target(
            client, args.base_url, fingerprint, args.plan_fingerprint
        )
    except DecisionError as exc:
        parser.error(str(exc))
    if payloads:
        result = client.save_activities(creates=payloads)
        errors = result.get("errors") or []
        if errors or len(result.get("created", [])) != len(payloads):
            parser.error(f"activity import failed: {errors[:1]}")
    client.post("/portfolio/recalculate", {})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
