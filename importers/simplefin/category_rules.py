"""Schema v2 staged rule engine for private SimpleFIN category decisions.

This module is deliberately independent of ``categorization.py`` so the
declarative rule model -- parsing, strict fail-closed validation, matching,
and a non-mutating v1-to-v2 migration proposal -- stays separately testable.
Nothing here ever stores or prints real merchant text; only synthetic
fixtures belong in this repository's tests and examples. Actual rule values
and traces live only in the private data directory.

Four non-recursive stages run in a fixed order for each transaction:

1. ``normalize``  -- may correct the normalized payee text used for matching.
2. ``classify``   -- may annotate a rule-local transaction kind and, only via
   the ``excluded`` value, remove a transaction from automatic categorization
   without touching the structural cash-flow classification owned by
   ``importers/normalized/builder.py``.
3. ``categorize`` -- may assign a taxonomy/category pair, exactly like a v1
   merchant/activity override.
4. ``decorate``   -- may propose tags/an event. Wealthfolio does not yet
   expose a supported write endpoint for these, so decorate actions are
   always recorded as unapplied in the trace.

Stages never call back into an earlier stage, so evaluation is a single
forward pass with no recursion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from importers.rebuild.decisions import DecisionError
from importers.simplefin.pipeline import normalize_description

SCHEMA_VERSION_V2 = 2
STAGES = ("normalize", "classify", "categorize", "decorate")
MAX_CONDITION_DEPTH = 2
#: A payee hash is a sha256 digest, so it is exactly 64 hexadecimal characters.
#: Length alone would accept any 64-character string.
SHA256_HEX = re.compile(r"[0-9a-f]{64}")
TEXT_OPS = frozenset({"exact", "contains", "startsWith", "regex"})
DIRECTIONS = frozenset({"debit", "credit"})
PENDING_STATES = frozenset({"pending", "posted"})
CASH_BUCKETS = frozenset({"income", "spending", "unclassified"})
CATEGORIZE_TAXONOMIES = frozenset({"spending_categories", "income_sources"})
# Mirrors milestone 2's canonical transaction_kind vocabulary. This module
# only *annotates* a rule-local classification for trace/decoration; it does
# not write canonical transaction_kind, which remains owned by
# importers/normalized/builder.py.
TRANSACTION_KINDS = frozenset({
    "expense", "income", "refund", "internal_transfer", "cc_payment",
    "loan_payment", "saving", "reimbursement", "reconciliation",
    "investment", "excluded",
})
_UNSET_KIND = "unset"


def _fail(message: str) -> None:
    raise DecisionError(f"category-decisions.json schema v2: {message}")


def _require_keys(payload: dict, allowed: set[str], context: str) -> None:
    if not isinstance(payload, dict):
        _fail(f"{context} must be an object")
    unknown = set(payload) - allowed
    if unknown:
        _fail(f"{context} has unknown field(s): {sorted(unknown)}")


@dataclass(frozen=True)
class Condition:
    kind: str  # "leaf" or "group"
    field_name: str | None = None
    op: str | None = None
    value: Any = None
    pattern: "re.Pattern[str] | None" = field(default=None, compare=False)
    group_op: str | None = None  # "all" | "any"
    children: tuple["Condition", ...] = ()


@dataclass(frozen=True)
class Action:
    type: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class Rule:
    id: str
    stage: str
    priority: int
    enabled: bool
    reviewed: bool
    stop_processing: bool
    rationale: str
    condition: Condition
    actions: tuple[Action, ...]


@dataclass(frozen=True)
class RuleEngine:
    rules: tuple[Rule, ...]

    def for_stage(self, stage: str) -> list[Rule]:
        return sorted(
            (rule for rule in self.rules if rule.stage == stage and rule.enabled),
            key=lambda rule: (rule.priority, rule.id),
        )


@dataclass
class RuleContext:
    """Per-transaction evaluation state threaded through the four stages."""

    payee: str
    payee_hash: str
    account_id: str
    activity_identity: dict[str, str]
    amount: Decimal
    direction: str | None
    cash_bucket: str
    date: str
    pending: bool = False
    transaction_kind: str = _UNSET_KIND
    excluded: bool = False
    excluded_rule_id: str | None = None
    tags: list[str] = field(default_factory=list)
    event: str | None = None


# --------------------------------------------------------------------------
# Parsing and strict, fail-closed validation
# --------------------------------------------------------------------------


def _compile_condition(node: Any, depth: int) -> Condition:
    if not isinstance(node, dict) or not node:
        _fail("condition must be a non-empty object")
    group_keys = {key for key in ("all", "any") if key in node}
    if group_keys:
        if len(group_keys) != 1:
            _fail("condition group must use exactly one of 'all' or 'any'")
        _require_keys(node, {"all", "any"}, "condition group")
        if depth > MAX_CONDITION_DEPTH:
            _fail(f"condition nesting exceeds supported depth {MAX_CONDITION_DEPTH}")
        group_op = next(iter(group_keys))
        children_raw = node[group_op]
        if not isinstance(children_raw, list) or not children_raw:
            _fail(f"condition '{group_op}' must be a non-empty list")
        children = tuple(
            _compile_condition(child, depth + 1) for child in children_raw
        )
        return Condition(kind="group", group_op=group_op, children=children)

    _require_keys(
        node,
        {"field", "op", "value", "min", "max", "start", "end"},
        "condition",
    )
    field_name = node.get("field")
    op = node.get("op")

    if field_name == "payee":
        if op not in TEXT_OPS:
            _fail(f"payee condition op must be one of {sorted(TEXT_OPS)}")
        value = node.get("value")
        if not isinstance(value, str) or not value:
            _fail("payee condition value must be a non-empty string")
        pattern = None
        if op == "regex":
            try:
                pattern = re.compile(value)
            except re.error as exc:
                _fail(f"invalid regex in payee condition: {exc}")
        return Condition(kind="leaf", field_name=field_name, op=op, value=value, pattern=pattern)

    if field_name == "payeeHash":
        if op != "exact":
            _fail("payeeHash condition only supports op 'exact'")
        value = node.get("value")
        if not isinstance(value, str) or not SHA256_HEX.fullmatch(value.casefold()):
            _fail(
                "payeeHash condition value must be a 64-character sha256 hex digest"
            )
        return Condition(kind="leaf", field_name=field_name, op=op, value=value.casefold())

    if field_name == "account":
        if op not in {"is", "in"}:
            _fail("account condition op must be 'is' or 'in'")
        value = node.get("value")
        if op == "is":
            if not isinstance(value, str) or not value:
                _fail("account condition value must be a non-empty string")
            values = (value,)
        else:
            if not isinstance(value, list) or not value or not all(
                isinstance(item, str) and item for item in value
            ):
                _fail("account condition 'in' value must be a non-empty list of strings")
            values = tuple(value)
        return Condition(kind="leaf", field_name=field_name, op=op, value=values)

    if field_name == "activityIdentity":
        if op != "exact":
            _fail("activityIdentity condition only supports op 'exact'")
        value = node.get("value")
        if (
            not isinstance(value, dict)
            or set(value) != {"sourceAccountId", "sourceId"}
            or not all(
                isinstance(value[key], str) and value[key] for key in value
            )
        ):
            _fail(
                "activityIdentity condition value requires non-empty "
                "sourceAccountId and sourceId"
            )
        return Condition(
            kind="leaf",
            field_name=field_name,
            op=op,
            value=(value["sourceAccountId"], value["sourceId"]),
        )

    if field_name == "cashBucket":
        if op not in {"is", "in"}:
            _fail("cashBucket condition op must be 'is' or 'in'")
        value = node.get("value")
        values = (value,) if op == "is" else tuple(value) if isinstance(value, list) else ()
        if not values or any(item not in CASH_BUCKETS for item in values):
            _fail(f"cashBucket condition value must be within {sorted(CASH_BUCKETS)}")
        return Condition(kind="leaf", field_name=field_name, op=op, value=values)

    if field_name == "transactionKind":
        if op not in {"is", "in"}:
            _fail("transactionKind condition op must be 'is' or 'in'")
        value = node.get("value")
        values = (value,) if op == "is" else tuple(value) if isinstance(value, list) else ()
        allowed = TRANSACTION_KINDS | {_UNSET_KIND}
        if not values or any(item not in allowed for item in values):
            _fail(f"transactionKind condition value must be within {sorted(allowed)}")
        return Condition(kind="leaf", field_name=field_name, op=op, value=values)

    if field_name == "amount":
        if op != "range":
            _fail("amount condition op must be 'range'")
        minimum = node.get("min")
        maximum = node.get("max")
        try:
            low = Decimal(str(minimum)) if minimum is not None else None
            high = Decimal(str(maximum)) if maximum is not None else None
        except InvalidOperation:
            _fail("amount condition min/max must be decimal strings")
        # Decimal() happily accepts "NaN", "Infinity" and "-Infinity". A NaN
        # bound makes every comparison in _match_leaf false, so the rule would
        # silently never fire; an infinite bound is a range nobody meant to
        # write. Both are rejected here rather than at match time.
        if (low is not None and not low.is_finite()) or (
            high is not None and not high.is_finite()
        ):
            _fail("amount condition min/max must be finite decimal strings")
        if low is None and high is None:
            _fail("amount condition requires min and/or max")
        if low is not None and high is not None and high < low:
            _fail("amount condition max must be on or after min")
        return Condition(kind="leaf", field_name=field_name, op=op, value=(low, high))

    if field_name == "direction":
        if op != "is":
            _fail("direction condition op must be 'is'")
        value = node.get("value")
        if value not in DIRECTIONS:
            _fail(f"direction condition value must be within {sorted(DIRECTIONS)}")
        return Condition(kind="leaf", field_name=field_name, op=op, value=value)

    if field_name == "date":
        if op != "range":
            _fail("date condition op must be 'range'")
        start = node.get("start")
        end = node.get("end")
        # Comparing the raw strings would accept "2024-99-99" and any other
        # impossible-but-well-ordered pair, so both endpoints are parsed as
        # real calendar dates and the ordering is checked on the dates.
        if not isinstance(start, str) or not isinstance(end, str):
            _fail("date condition requires start on or before end, both ISO dates")
        try:
            first = date.fromisoformat(start)
            last = date.fromisoformat(end)
        except ValueError:
            _fail("date condition requires start on or before end, both ISO dates")
        if last < first:
            _fail("date condition requires start on or before end, both ISO dates")
        return Condition(
            kind="leaf",
            field_name=field_name,
            op=op,
            value=(first.isoformat(), last.isoformat()),
        )

    if field_name == "pendingState":
        if op != "is":
            _fail("pendingState condition op must be 'is'")
        value = node.get("value")
        if value not in PENDING_STATES:
            _fail(f"pendingState condition value must be within {sorted(PENDING_STATES)}")
        return Condition(kind="leaf", field_name=field_name, op=op, value=value)

    _fail(f"condition has unknown field: {field_name!r}")
    raise AssertionError("unreachable")  # pragma: no cover - _fail always raises


def _compile_action(node: Any, stage: str) -> Action:
    if not isinstance(node, dict):
        _fail("action must be an object")
    action_type = node.get("type")
    if stage == "normalize":
        if action_type != "setPayee":
            _fail(f"normalize stage only supports setPayee actions, got {action_type!r}")
        _require_keys(node, {"type", "value"}, "setPayee action")
        value = node.get("value")
        if not isinstance(value, str) or not value.strip():
            _fail("setPayee action value must be a non-empty string")
        return Action(type=action_type, payload={"value": value})

    if stage == "classify":
        if action_type != "setTransactionKind":
            _fail(
                f"classify stage only supports setTransactionKind actions, "
                f"got {action_type!r}"
            )
        _require_keys(node, {"type", "value"}, "setTransactionKind action")
        value = node.get("value")
        if value not in TRANSACTION_KINDS:
            _fail(f"setTransactionKind action value must be within {sorted(TRANSACTION_KINDS)}")
        return Action(type=action_type, payload={"value": value})

    if stage == "categorize":
        if action_type != "setCategory":
            _fail(f"categorize stage only supports setCategory actions, got {action_type!r}")
        _require_keys(node, {"type", "taxonomyId", "categoryId"}, "setCategory action")
        taxonomy_id = node.get("taxonomyId")
        category_id = node.get("categoryId")
        if (
            taxonomy_id not in CATEGORIZE_TAXONOMIES
            or not isinstance(category_id, str)
            or not category_id
        ):
            _fail("setCategory action requires a known taxonomyId and non-empty categoryId")
        return Action(
            type=action_type,
            payload={"taxonomyId": taxonomy_id, "categoryId": category_id},
        )

    if stage == "decorate":
        if action_type not in {"addTag", "setEvent"}:
            _fail(f"decorate stage only supports addTag/setEvent actions, got {action_type!r}")
        _require_keys(node, {"type", "value"}, f"{action_type} action")
        value = node.get("value")
        if not isinstance(value, str) or not value.strip():
            _fail(f"{action_type} action value must be a non-empty string")
        return Action(type=action_type, payload={"value": value})

    _fail(f"unknown stage: {stage!r}")
    raise AssertionError("unreachable")  # pragma: no cover - _fail always raises


def _collect_directions(condition: Condition) -> set[str]:
    if condition.kind == "leaf":
        return {condition.value} if condition.field_name == "direction" else set()
    result: set[str] = set()
    for child in condition.children:
        result |= _collect_directions(child)
    return result


def _check_direction_conflict(
    rule_id: str, condition: Condition, actions: tuple[Action, ...]
) -> None:
    directions = _collect_directions(condition)
    if not directions:
        return
    for action in actions:
        if action.type != "setCategory":
            continue
        taxonomy_id = action.payload["taxonomyId"]
        if "debit" in directions and taxonomy_id == "income_sources":
            _fail(
                f"rule {rule_id!r} direction condition 'debit' conflicts with "
                "an income category action"
            )
        if "credit" in directions and taxonomy_id == "spending_categories":
            _fail(
                f"rule {rule_id!r} direction condition 'credit' conflicts with "
                "a spending category action"
            )


_RULE_ALLOWED_KEYS = {
    "id", "stage", "priority", "enabled", "reviewed", "stopProcessing",
    "rationale", "when", "actions",
}


def _compile_rule(node: Any, seen_ids: set[str]) -> Rule:
    if not isinstance(node, dict):
        _fail("rule must be an object")
    _require_keys(node, _RULE_ALLOWED_KEYS, "rule")

    rule_id = node.get("id")
    if not isinstance(rule_id, str) or not rule_id.strip():
        _fail("rule id must be a non-empty string")
    if rule_id in seen_ids:
        _fail(f"duplicate rule id: {rule_id!r}")
    seen_ids.add(rule_id)

    stage = node.get("stage")
    if stage not in STAGES:
        _fail(f"rule {rule_id!r} stage must be one of {STAGES}")

    priority = node.get("priority")
    if not isinstance(priority, int) or isinstance(priority, bool):
        _fail(f"rule {rule_id!r} priority must be an integer")

    enabled = node.get("enabled", True)
    if not isinstance(enabled, bool):
        _fail(f"rule {rule_id!r} enabled must be a boolean")

    reviewed = node.get("reviewed")
    if not isinstance(reviewed, bool):
        _fail(f"rule {rule_id!r} reviewed must be an explicit boolean")

    stop_processing = node.get("stopProcessing", False)
    if not isinstance(stop_processing, bool):
        _fail(f"rule {rule_id!r} stopProcessing must be a boolean")

    rationale = node.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        _fail(f"rule {rule_id!r} requires a non-empty rationale")

    when = node.get("when")
    if when is None:
        _fail(f"rule {rule_id!r} requires a 'when' condition")
    condition = _compile_condition(when, depth=1)

    actions_raw = node.get("actions")
    if not isinstance(actions_raw, list) or not actions_raw:
        _fail(f"rule {rule_id!r} requires a non-empty actions list")
    actions = tuple(_compile_action(action, stage) for action in actions_raw)
    _check_direction_conflict(rule_id, condition, actions)

    return Rule(
        id=rule_id,
        stage=stage,
        priority=priority,
        enabled=enabled,
        reviewed=reviewed,
        stop_processing=stop_processing,
        rationale=rationale,
        condition=condition,
        actions=actions,
    )


def parse_rule_engine(payload: dict[str, Any]) -> RuleEngine:
    """Strictly, fail-closed parse and validate a schema v2 document body."""
    _require_keys(
        payload, {"schemaVersion", "rules", "categoryAliases", "_comment"}, "category-decisions.json"
    )
    if payload.get("schemaVersion") != SCHEMA_VERSION_V2:
        _fail(f"schemaVersion must be {SCHEMA_VERSION_V2}")
    rules_raw = payload.get("rules")
    if not isinstance(rules_raw, list):
        _fail("'rules' must be a list")
    seen_ids: set[str] = set()
    rules = tuple(_compile_rule(rule, seen_ids) for rule in rules_raw)
    return RuleEngine(rules=rules)


# --------------------------------------------------------------------------
# Matching and stage evaluation
# --------------------------------------------------------------------------


def _match_leaf(condition: Condition, context: RuleContext) -> bool:
    field_name = condition.field_name
    if field_name == "payee":
        text = context.payee
        if condition.op == "exact":
            return text == normalize_description(condition.value)
        if condition.op == "contains":
            return normalize_description(condition.value) in text
        if condition.op == "startsWith":
            return text.startswith(normalize_description(condition.value))
        if condition.op == "regex":
            return condition.pattern.search(text) is not None
        return False
    if field_name == "payeeHash":
        return context.payee_hash.casefold() == condition.value
    if field_name == "account":
        return context.account_id in condition.value
    if field_name == "activityIdentity":
        identity = (
            context.activity_identity.get("sourceAccountId"),
            context.activity_identity.get("sourceId"),
        )
        return identity == condition.value
    if field_name == "cashBucket":
        return context.cash_bucket in condition.value
    if field_name == "transactionKind":
        return context.transaction_kind in condition.value
    if field_name == "amount":
        low, high = condition.value
        if low is not None and context.amount < low:
            return False
        if high is not None and context.amount > high:
            return False
        return True
    if field_name == "direction":
        return context.direction == condition.value
    if field_name == "date":
        start, end = condition.value
        return start <= context.date <= end
    if field_name == "pendingState":
        state = "pending" if context.pending else "posted"
        return state == condition.value
    return False


def _match(condition: Condition, context: RuleContext) -> bool:
    if condition.kind == "leaf":
        return _match_leaf(condition, context)
    if condition.group_op == "all":
        return all(_match(child, context) for child in condition.children)
    return any(_match(child, context) for child in condition.children)


def run_normalize_and_classify(
    engine: RuleEngine, context: RuleContext
) -> dict[str, list[dict[str, Any]]]:
    """Run stages 1-2, mutating ``context`` in place. Returns their traces."""
    normalize_trace: list[dict[str, Any]] = []
    for rule in engine.for_stage("normalize"):
        if not _match(rule.condition, context):
            continue
        for action in rule.actions:
            if action.type == "setPayee":
                context.payee = normalize_description(action.payload["value"])
        normalize_trace.append({"ruleId": rule.id, "reviewed": rule.reviewed})
        if rule.stop_processing:
            break

    classify_trace: list[dict[str, Any]] = []
    for rule in engine.for_stage("classify"):
        if not _match(rule.condition, context):
            continue
        for action in rule.actions:
            if action.type == "setTransactionKind":
                context.transaction_kind = action.payload["value"]
                if action.payload["value"] == "excluded" and not context.excluded:
                    context.excluded = True
                    context.excluded_rule_id = rule.id
        classify_trace.append({"ruleId": rule.id, "reviewed": rule.reviewed})
        if rule.stop_processing:
            break

    return {"normalize": normalize_trace, "classify": classify_trace}


def run_categorize(
    engine: RuleEngine, context: RuleContext
) -> tuple[dict[str, str] | None, str | None, bool, list[dict[str, Any]]]:
    """Run stage 3. Returns (category action payload, rule id, reviewed, trace)."""
    trace: list[dict[str, Any]] = []
    payload: dict[str, str] | None = None
    rule_id: str | None = None
    reviewed = False
    for rule in engine.for_stage("categorize"):
        if not _match(rule.condition, context):
            continue
        for action in rule.actions:
            if action.type == "setCategory":
                payload = dict(action.payload)
                rule_id = rule.id
                reviewed = rule.reviewed
        trace.append({"ruleId": rule.id, "reviewed": rule.reviewed})
        if rule.stop_processing:
            break
    return payload, rule_id, reviewed, trace


def run_decorate(engine: RuleEngine, context: RuleContext) -> list[dict[str, Any]]:
    """Run stage 4, mutating ``context.tags``/``context.event``.

    Wealthfolio does not currently expose a supported tag/event write
    endpoint, so every decorate action is recorded as unapplied.
    """
    trace: list[dict[str, Any]] = []
    for rule in engine.for_stage("decorate"):
        if not _match(rule.condition, context):
            continue
        actions_trace = []
        for action in rule.actions:
            if action.type == "addTag":
                context.tags.append(action.payload["value"])
            elif action.type == "setEvent":
                context.event = action.payload["value"]
            actions_trace.append({
                "type": action.type,
                "applied": False,
                "reason": "unsupported-destination",
            })
        trace.append({
            "ruleId": rule.id,
            "reviewed": rule.reviewed,
            "actions": actions_trace,
        })
        if rule.stop_processing:
            break
    return trace


# --------------------------------------------------------------------------
# Non-mutating v1 -> v2 migration proposal
# --------------------------------------------------------------------------


def propose_v2_from_v1(v1_decisions: dict[str, Any]) -> dict[str, Any]:
    """Build a reviewable v2 proposal from v1 aliases/overrides.

    Never mutates or reads back the source file; the caller decides whether
    and where to persist the returned document. ``categoryAliases`` is
    generic vocabulary translation, not a per-transaction rule, so it is
    carried through unchanged rather than converted into rules.
    """
    if v1_decisions.get("schemaVersion") != 1:
        raise DecisionError("migration source must be a schemaVersion 1 document")

    rules: list[dict[str, Any]] = []
    priority = 100
    for index, override in enumerate(v1_decisions.get("merchantOverrides", [])):
        merchant_hash = str(override.get("merchantHash") or "").casefold()
        if not SHA256_HEX.fullmatch(merchant_hash):
            raise DecisionError(
                "migration source has a merchant override without a "
                "64-character sha256 hex digest"
            )
        condition: dict[str, Any] = {
            "field": "payeeHash",
            "op": "exact",
            "value": merchant_hash,
        }
        when = condition
        if override.get("canonicalAccountId"):
            when = {
                "all": [
                    condition,
                    {
                        "field": "account",
                        "op": "is",
                        "value": str(override["canonicalAccountId"]),
                    },
                ]
            }
        rules.append({
            "id": f"migrated-merchant-override-{index + 1}",
            "stage": "categorize",
            "priority": priority,
            "enabled": True,
            "reviewed": True,
            "stopProcessing": True,
            "rationale": str(
                override.get("rationale") or "migrated from v1 merchantOverrides"
            ),
            "when": when,
            "actions": [{
                "type": "setCategory",
                "taxonomyId": str(override.get("taxonomyId") or ""),
                "categoryId": str(override.get("categoryId") or ""),
            }],
        })
        priority += 1

    for index, override in enumerate(v1_decisions.get("activityOverrides", [])):
        rules.append({
            "id": f"migrated-activity-override-{index + 1}",
            "stage": "categorize",
            "priority": priority,
            "enabled": True,
            "reviewed": True,
            "stopProcessing": True,
            "rationale": str(
                override.get("rationale") or "migrated from v1 activityOverrides"
            ),
            "when": {
                "field": "activityIdentity",
                "op": "exact",
                "value": {
                    "sourceAccountId": str(override.get("sourceAccountId") or ""),
                    "sourceId": str(override.get("sourceId") or ""),
                },
            },
            "actions": [{
                "type": "setCategory",
                "taxonomyId": str(override.get("taxonomyId") or ""),
                "categoryId": str(override.get("categoryId") or ""),
            }],
        })
        priority += 1

    proposal = {
        "schemaVersion": SCHEMA_VERSION_V2,
        "categoryAliases": v1_decisions.get("categoryAliases", {}),
        "rules": rules,
    }
    # Re-validate the proposal so a malformed v1 source cannot silently
    # produce an invalid v2 document; this never touches the v1 source.
    parse_rule_engine(proposal)
    return proposal
