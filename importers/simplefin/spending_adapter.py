"""Capability-gated adapter around Wealthfolio's Spending endpoints.

Every ``/taxonomies/*``, ``/spending/*``, and per-activity assignment call used
by the SimpleFIN categorization tools is made through this module instead of
being scattered across ``categorize_cli.py``, ``categorization.py``, and
``apply_cli.py``. A capability is either verified available before it is relied
upon (read endpoints, which are safe to probe) or classified on its first real
use (write endpoints, which cannot be probed without a genuine object to
mutate). Either way, an unsupported or broken capability raises
``SpendingCapabilityBlocked`` -- a ``DecisionError`` subclass carrying a
structured, actionable ``CapabilityStatus`` -- instead of a raw HTTP failure.

The endpoint table below was read from the pinned upstream source
(``wealthfolio/wealthfolio`` tag ``v3.7.0``): ``apps/server/src/api/spending.rs``
and ``apps/server/src/api/taxonomies.rs`` for the routes, the matching
``crates/spending`` and ``crates/core/src/taxonomies`` models for request and
response shapes, and ``apps/frontend/src/adapters/web/core.ts`` for the exact
URL and body construction the official web client uses. Nothing here is a
guessed path.

Configuration writes (taxonomy/category, categorization rules, budgets) are
locked off by default. They are only reachable on an adapter constructed with
``allow_configuration_writes=True``, so importing this module can never mutate
a live instance by accident.

Wealthfolio's SQLite database is never opened, read, or written anywhere in
this module or its callers; every operation is an authenticated REST call to
the running application.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable
from urllib.parse import quote, urlencode

from importers.monarch.wealthfolio_client import WealthfolioClient, WealthfolioError
from importers.rebuild.decisions import DecisionError

SPENDING_TAXONOMY = "spending_categories"
INCOME_TAXONOMY = "income_sources"
UNCATEGORIZED_CATEGORY_IDS = frozenset({"__uncategorized__", ""})

# Budget period keys are either the literal cross-month default or a month key.
DEFAULT_PERIOD_KEY = "default"

BUDGET_TARGET_TYPES = frozenset({"category", "group_buffer"})
BUDGET_ROLLOVER_TARGET_TYPES = frozenset({"category", "group"})
RULE_MATCH_TYPES = frozenset({"contains", "starts_with", "exact", "regex"})

# -- reads -------------------------------------------------------------------
CAP_TAXONOMY_LIST = "taxonomyList"
CAP_TAXONOMY_READ = "taxonomyRead"
CAP_SETTINGS_READ = "spendingSettingsRead"
CAP_REPORT_READ = "spendingReportRead"
CAP_SEARCH_READ = "spendingSearchRead"
CAP_ASSIGNMENT_READ = "activityAssignmentRead"
CAP_RULE_READ = "categorizationRuleRead"
CAP_BUDGET_READ = "budgetRead"
CAP_BACKUP_READ = "databaseBackupRead"

# -- activity assignment writes ----------------------------------------------
CAP_ASSIGNMENT_WRITE = "activityAssignmentWrite"
CAP_ASSIGNMENT_DELETE = "activityAssignmentDelete"

# -- taxonomy/category configuration writes ----------------------------------
CAP_CATEGORY_CREATE = "taxonomyCategoryCreate"
CAP_CATEGORY_UPDATE = "taxonomyCategoryUpdate"
CAP_CATEGORY_MOVE = "taxonomyCategoryMove"
CAP_CATEGORY_DELETE = "taxonomyCategoryDelete"

# -- categorization-rule configuration writes --------------------------------
CAP_RULE_WRITE = "categorizationRuleWrite"
CAP_RULE_UPDATE = "categorizationRuleUpdate"
CAP_RULE_DELETE = "categorizationRuleDelete"
CAP_RULE_RERUN = "categorizationRuleRerun"

# -- budget configuration writes ---------------------------------------------
CAP_BUDGET_TARGET_WRITE = "budgetTargetWrite"
CAP_BUDGET_TARGET_DELETE = "budgetTargetDelete"
CAP_BUDGET_ROLLOVER_WRITE = "budgetRolloverWrite"
CAP_BUDGET_ROLLOVER_DELETE = "budgetRolloverDelete"
CAP_BUDGET_GROUP_CREATE = "budgetGroupCreate"
CAP_BUDGET_GROUP_UPDATE = "budgetGroupUpdate"
CAP_BUDGET_GROUP_DELETE = "budgetGroupDelete"
CAP_BUDGET_GROUP_ASSIGN = "budgetGroupAssign"
CAP_BUDGET_COPY = "budgetTargetCopy"

CAP_BACKUP = "databaseBackup"

# -- genuine gaps in the pinned version --------------------------------------
CAP_TAXONOMY_MERGE_IMPORT = "taxonomyMergeImport"
CAP_CATEGORY_PATCH = "taxonomyCategoryPatch"
CAP_CATEGORY_READ_ONE = "taxonomyCategoryReadOne"
CAP_BUDGET_PERIOD_LIST = "budgetPeriodList"

#: Every REST route this adapter is allowed to call, verified against the
#: pinned Wealthfolio source. ``{}`` marks a path segment filled in per call.
SUPPORTED_ENDPOINTS: dict[str, tuple[str, str]] = {
    CAP_TAXONOMY_LIST: ("GET", "/taxonomies"),
    CAP_TAXONOMY_READ: ("GET", "/taxonomies/{taxonomyId}"),
    CAP_CATEGORY_CREATE: ("POST", "/taxonomies/categories"),
    CAP_CATEGORY_UPDATE: ("PUT", "/taxonomies/categories"),
    CAP_CATEGORY_MOVE: ("POST", "/taxonomies/categories/move"),
    CAP_CATEGORY_DELETE: ("DELETE", "/taxonomies/{taxonomyId}/categories/{categoryId}"),
    CAP_SETTINGS_READ: ("GET", "/spending/settings"),
    CAP_REPORT_READ: ("POST", "/spending/report"),
    CAP_SEARCH_READ: ("POST", "/spending/cash-activities/search"),
    CAP_ASSIGNMENT_READ: ("GET", "/spending/activities/{activityId}/assignments"),
    CAP_ASSIGNMENT_WRITE: ("PUT", "/spending/activities/{activityId}/assignments"),
    CAP_ASSIGNMENT_DELETE: (
        "DELETE",
        "/spending/activities/{activityId}/assignments/{taxonomyId}",
    ),
    CAP_RULE_READ: ("GET", "/spending/rules"),
    CAP_RULE_WRITE: ("POST", "/spending/rules"),
    CAP_RULE_UPDATE: ("PUT", "/spending/rules/{ruleId}"),
    CAP_RULE_DELETE: ("DELETE", "/spending/rules/{ruleId}"),
    CAP_RULE_RERUN: ("POST", "/spending/rules/rerun"),
    CAP_BUDGET_READ: ("GET", "/spending/budget"),
    CAP_BUDGET_TARGET_WRITE: ("POST", "/spending/budget/targets"),
    CAP_BUDGET_TARGET_DELETE: ("DELETE", "/spending/budget/targets/{targetId}"),
    CAP_BUDGET_ROLLOVER_WRITE: ("POST", "/spending/budget/rollovers"),
    CAP_BUDGET_ROLLOVER_DELETE: ("DELETE", "/spending/budget/rollovers/{settingId}"),
    CAP_BUDGET_GROUP_CREATE: ("POST", "/spending/budget/groups"),
    CAP_BUDGET_GROUP_UPDATE: ("PUT", "/spending/budget/groups/{groupId}"),
    CAP_BUDGET_GROUP_DELETE: ("DELETE", "/spending/budget/groups/{groupId}"),
    CAP_BUDGET_GROUP_ASSIGN: ("POST", "/spending/budget/group-assignments"),
    CAP_BUDGET_COPY: ("POST", "/spending/budget/copy"),
    CAP_BACKUP: ("POST", "/utilities/database/backup"),
    CAP_BACKUP_READ: ("GET", "/utilities/database/backups"),
}

#: Capabilities this repository never attempts because the pinned Wealthfolio
#: build genuinely exposes no endpoint for them. Each entry was confirmed
#: absent from the v3.7.0 routers, not merely undocumented. Recorded once, so
#: every caller gets the same actionable explanation instead of a guessed
#: request against a path that does not exist.
KNOWN_API_GAPS: dict[str, str] = {
    CAP_TAXONOMY_MERGE_IMPORT: (
        "POST /taxonomies/import always creates a new, non-system taxonomy; "
        "there is no endpoint that merges a taxonomy JSON document into the "
        "existing system 'spending_categories' taxonomy. Hierarchy changes "
        "must be applied one category at a time through the supported "
        "create/update/move category endpoints."
    ),
    CAP_CATEGORY_PATCH: (
        "There is no partial-update endpoint for a category. "
        "PUT /taxonomies/categories takes a complete Category object "
        "(including id, key, color, sortOrder, createdAt and updatedAt), so a "
        "rename or recolour must read the live category first and send the "
        "whole row back."
    ),
    CAP_CATEGORY_READ_ONE: (
        "There is no endpoint that returns a single category by id. A "
        "category is only reachable by reading its whole taxonomy through "
        "GET /taxonomies/{taxonomyId}."
    ),
    CAP_BUDGET_PERIOD_LIST: (
        "There is no endpoint that enumerates configured budget periods. "
        "GET /spending/budget resolves exactly one periodKey ('default' or "
        "YYYY-MM), defaulting to the current month in the instance timezone, "
        "so the caller must already know which period it wants."
    ),
}


def validate_period_key(period_key: str | None) -> str | None:
    """Reject anything Wealthfolio would reject, before it reaches the wire.

    ``None`` means "let the server pick the current month", which is the
    documented behaviour of an omitted ``periodKey`` query parameter.
    """
    if period_key is None:
        return None
    if not isinstance(period_key, str):
        raise DecisionError(f"invalid budget period key: {period_key!r}")
    if period_key == DEFAULT_PERIOD_KEY:
        return period_key
    if len(period_key) != 7 or period_key[4] != "-":
        raise DecisionError(f"invalid budget period key: {period_key!r}")
    year, month = period_key[:4], period_key[5:]
    if not (year.isdigit() and month.isdigit()):
        raise DecisionError(f"invalid budget period key: {period_key!r}")
    if not 1 <= int(month) <= 12:
        raise DecisionError(f"invalid budget period key: {period_key!r}")
    return period_key


# --------------------------------------------------------------------------
# Endpoint-specific structural validators
# --------------------------------------------------------------------------
# Each returns ``None`` when a payload is genuinely usable, or a short reason
# why it is not. They are pure functions of the response shape and never echo
# a value, so a reason is always safe to publish. ``SpendingAdapter`` raises
# ``SpendingCapabilityBlocked`` from them, and
# ``importers/analytics/diagnostics.py`` reuses them so a capability reported
# there as "available" means exactly what it means here: an empty ``{}`` for
# ``/spending/budget`` or ``/spending/report`` is incompatible, not available.


def settings_problem(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return "spending settings response is not an object"
    if "enabled" in payload and not isinstance(payload["enabled"], bool):
        return "spending settings 'enabled' is not a boolean"
    account_ids = payload.get("accountIds")
    if not isinstance(account_ids, list):
        return "spending settings has no 'accountIds' array"
    if any(not isinstance(account_id, str) for account_id in account_ids):
        return "spending settings 'accountIds' contains a non-string"
    return None


def report_problem(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return "spending report response is not an object"
    if not isinstance(payload.get("current"), dict):
        return "spending report has no 'current' totals object"
    breakdown = payload.get("spendingBreakdown")
    if breakdown is not None and (
        not isinstance(breakdown, list)
        or any(not isinstance(row, dict) for row in breakdown)
    ):
        return "spending report 'spendingBreakdown' is not an array of objects"
    return None


def rule_list_problem(payload: Any) -> str | None:
    if not isinstance(payload, list):
        return "rule response is not an array"
    if any(not isinstance(row, dict) for row in payload):
        return "rule list contains a non-object"
    return None


def taxonomy_categories_problem(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return "taxonomy response is not an object"
    categories = payload.get("categories")
    if not isinstance(categories, list):
        return "response has no categories array"
    if any(not isinstance(category, dict) for category in categories):
        return "categories array contains a non-object"
    return None


def budget_snapshot_problem(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return "budget response is not an object"
    state = payload.get("state")
    computed = payload.get("computed")
    if not isinstance(state, dict) or not isinstance(computed, dict):
        return "budget response has no state/computed objects"
    for field in ("groups", "groupAssignments", "targets", "rolloverSettings"):
        if not isinstance(state.get(field), list):
            return f"budget state.{field} is not an array"
    for field in ("groupRows", "ungroupedRows", "incomeRows"):
        if not isinstance(computed.get(field), list):
            return f"budget computed.{field} is not an array"
    if not isinstance(computed.get("totals"), dict):
        return "budget computed.totals is not an object"
    if not isinstance(computed.get("periodKey"), str) or not computed["periodKey"]:
        return "budget computed.periodKey is missing"
    return None


@dataclass(frozen=True)
class CapabilityStatus:
    """The result of checking one Spending capability."""

    capability: str
    # "available" | "unsupported" | "incompatible" | "error" | "locked"
    status: str
    endpoint: str
    detail: str = ""

    @property
    def available(self) -> bool:
        return self.status == "available"

    @property
    def explanation(self) -> str:
        return KNOWN_API_GAPS.get(self.capability, self.detail) or self.status


class SpendingCapabilityBlocked(DecisionError):
    """A required Spending capability is unsupported, broken, or locked."""

    def __init__(self, status: CapabilityStatus):
        message = (
            f"Wealthfolio Spending capability blocked: {status.capability} "
            f"is {status.status} at {status.endpoint} ({status.explanation})"
        )
        super().__init__(message)
        self.status = status


def _classify_http_error(exc: WealthfolioError) -> str:
    return "unsupported" if exc.status in {404, 405} else "error"


def _q(value: str) -> str:
    return quote(str(value), safe="")


class SpendingAdapter:
    """Capability-gated facade over the Wealthfolio Spending API.

    Accepts anything duck-typed like ``WealthfolioClient`` (``.get``,
    ``.post``, ``.put``, ``.delete``, ``.backup_database``), so synthetic
    HTTP/client fakes used by contract tests need only implement the methods a
    given code path actually exercises. Every operation makes exactly one HTTP
    call: success or failure is classified inline and cached, so a capability
    is never probed separately from actually being used.

    ``allow_configuration_writes`` gates the taxonomy, categorization-rule and
    budget mutation surface. It defaults to ``False``: those methods raise
    ``SpendingCapabilityBlocked`` with status ``locked`` and make no network
    call at all, so read-only tooling can never reshape a live instance.
    """

    def __init__(
        self,
        client: WealthfolioClient,
        *,
        allow_configuration_writes: bool = False,
    ):
        self._client = client
        self._allow_configuration_writes = bool(allow_configuration_writes)
        self._cache: dict[str, CapabilityStatus] = {}

    @property
    def configuration_writes_allowed(self) -> bool:
        return self._allow_configuration_writes

    # -- shared call/classification plumbing ---------------------------------

    def _call(self, capability: str, endpoint: str, func):
        try:
            result = func()
        except WealthfolioError as exc:
            status = CapabilityStatus(capability, _classify_http_error(exc), endpoint, str(exc))
            self._cache[capability] = status
            raise SpendingCapabilityBlocked(status) from None
        self._cache.setdefault(capability, CapabilityStatus(capability, "available", endpoint))
        return result

    def _incompatible(self, capability: str, endpoint: str, detail: str) -> None:
        status = CapabilityStatus(capability, "incompatible", endpoint, detail)
        self._cache[capability] = status
        raise SpendingCapabilityBlocked(status)

    def _read(self, capability: str, endpoint: str, func, expected_type: type):
        result = self._call(capability, endpoint, func)
        if result is None:
            result = expected_type()
        if not isinstance(result, expected_type):
            self._incompatible(
                capability, endpoint, f"{endpoint} response is not a {expected_type.__name__}"
            )
        return result

    def _configuration_write(
        self,
        capability: str,
        endpoint: str,
        func,
        validator: Callable[[Any, str, str], Any] | None = None,
    ):
        """Run one gated configuration mutation, validating what comes back."""
        if not self._allow_configuration_writes:
            status = CapabilityStatus(
                capability,
                "locked",
                endpoint,
                "configuration writes are disabled on this adapter; construct "
                "SpendingAdapter(..., allow_configuration_writes=True) only "
                "from a deliberate, operator-confirmed apply path",
            )
            self._cache.setdefault(capability, status)
            raise SpendingCapabilityBlocked(status)
        result = self._call(capability, endpoint, func)
        if validator is None:
            return result
        return validator(result, capability, endpoint)

    def status(self, capability: str) -> CapabilityStatus | None:
        return self._cache.get(capability)

    def require(self, capability: str, *, endpoint: str = "") -> CapabilityStatus:
        """Raise if `capability` is known-blocked or has never been probed.

        Read/write methods below classify themselves as they are used, so
        this is only needed by a caller that wants to check a capability
        without performing its operation yet (e.g. before building a plan).
        """
        cached = self._cache.get(capability)
        if cached is None:
            self.capabilities()
            cached = self._cache.get(capability)
        if cached is None or not cached.available:
            raise SpendingCapabilityBlocked(
                cached
                or CapabilityStatus(
                    capability,
                    "unsupported",
                    endpoint or SUPPORTED_ENDPOINTS.get(capability, ("", ""))[1],
                    "not probed",
                )
            )
        return cached

    def capabilities(self, *, refresh: bool = False) -> dict[str, CapabilityStatus]:
        """Probe every read-safe capability. Never mutates Wealthfolio state.

        Write capabilities that cannot be probed without a real object to
        mutate (assignment writes, category/rule/budget configuration writes)
        or a real backup request are absent here and are classified lazily,
        on first genuine use, instead. The genuine API gaps of the pinned
        version are recorded without any network call.
        """
        if refresh:
            self._cache.clear()
        dummy_window = {"startDate": "1970-01-01T00:00:00Z", "endDate": "1970-01-31T23:59:59Z"}
        probes = (
            self.settings,
            lambda: self.report(dict(dummy_window)),
            lambda: self.uncategorized_count(dict(dummy_window)),
            self.taxonomies,
            lambda: self.taxonomy(SPENDING_TAXONOMY),
            lambda: self.taxonomy(INCOME_TAXONOMY),
            self.rules,
            self.budget,
        )
        for probe in probes:
            try:
                probe()
            except SpendingCapabilityBlocked:
                pass  # already recorded in self._cache by the probe itself
        for capability, explanation in KNOWN_API_GAPS.items():
            self._cache.setdefault(
                capability,
                CapabilityStatus(capability, "unsupported", "(no route)", explanation),
            )
        return dict(self._cache)

    def known_gaps(self) -> dict[str, str]:
        """The genuine, verified API gaps of the pinned Wealthfolio version."""
        return dict(KNOWN_API_GAPS)

    # -- response validation --------------------------------------------------

    def _require_object(self, payload: Any, capability: str, endpoint: str) -> dict[str, Any]:
        if not isinstance(payload, dict):
            self._incompatible(capability, endpoint, f"{endpoint} response is not an object")
        return payload

    def _validate_category(self, payload: Any, capability: str, endpoint: str) -> dict[str, Any]:
        category = self._require_object(payload, capability, endpoint)
        for field in ("id", "taxonomyId", "name", "key"):
            if not isinstance(category.get(field), str) or not category[field]:
                self._incompatible(
                    capability, endpoint, f"category response is missing a usable {field}"
                )
        parent = category.get("parentId")
        if parent is not None and not isinstance(parent, str):
            self._incompatible(
                capability, endpoint, "category parentId is neither null nor a string"
            )
        if not isinstance(category.get("sortOrder"), int) or isinstance(
            category.get("sortOrder"), bool
        ):
            self._incompatible(capability, endpoint, "category sortOrder is not an integer")
        return category

    def _validate_rule(self, payload: Any, capability: str, endpoint: str) -> dict[str, Any]:
        rule = self._require_object(payload, capability, endpoint)
        for field in ("id", "name", "pattern"):
            if not isinstance(rule.get(field), str) or not rule[field]:
                self._incompatible(
                    capability, endpoint, f"rule response is missing a usable {field}"
                )
        if rule.get("matchType") not in RULE_MATCH_TYPES:
            self._incompatible(
                capability,
                endpoint,
                f"rule response has unknown matchType {rule.get('matchType')!r}",
            )
        return rule

    def _validate_budget_snapshot(
        self, payload: Any, capability: str, endpoint: str
    ) -> dict[str, Any]:
        snapshot = self._require_object(payload, capability, endpoint)
        problem = budget_snapshot_problem(snapshot)
        if problem:
            self._incompatible(capability, endpoint, problem)
        return snapshot

    # -- reads ----------------------------------------------------------------

    def taxonomies(self) -> list[dict[str, Any]]:
        """List every taxonomy (asset- and activity-scoped)."""
        endpoint = "/taxonomies"
        rows = self._read(CAP_TAXONOMY_LIST, endpoint, lambda: self._client.get(endpoint), list)
        if any(not isinstance(row, dict) for row in rows):
            self._incompatible(CAP_TAXONOMY_LIST, endpoint, "taxonomy list contains a non-object")
        return rows

    def taxonomy_detail(self, taxonomy_id: str) -> dict[str, Any]:
        """Return the full ``TaxonomyWithCategories`` document for one taxonomy.

        The pinned server serialises ``Option<TaxonomyWithCategories>``, so a
        missing taxonomy comes back as JSON ``null`` with a 200 status. That is
        an incompatible response for a caller that asked for a specific
        taxonomy, not an absent endpoint.
        """
        capability = f"{CAP_TAXONOMY_READ}:{taxonomy_id}"
        endpoint = f"/taxonomies/{_q(taxonomy_id)}"
        payload = self._call(capability, endpoint, lambda: self._client.get(endpoint))
        detail = self._require_object(payload, capability, endpoint)
        if not isinstance(detail.get("taxonomy"), dict):
            self._incompatible(capability, endpoint, "response has no taxonomy object")
        problem = taxonomy_categories_problem(detail)
        if problem:
            self._incompatible(capability, endpoint, problem)
        return detail

    def taxonomy(self, taxonomy_id: str) -> list[dict[str, Any]]:
        capability = f"{CAP_TAXONOMY_READ}:{taxonomy_id}"
        endpoint = f"/taxonomies/{_q(taxonomy_id)}"
        payload = self._read(capability, endpoint, lambda: self._client.get(endpoint), dict)
        problem = taxonomy_categories_problem(payload)
        if problem:
            self._incompatible(capability, endpoint, problem)
        return payload["categories"]

    def catalogs(self) -> dict[str, list[dict[str, Any]]]:
        return {
            taxonomy_id: self.taxonomy(taxonomy_id)
            for taxonomy_id in (SPENDING_TAXONOMY, INCOME_TAXONOMY)
        }

    def settings(self) -> dict[str, Any]:
        endpoint = "/spending/settings"
        return self._read(CAP_SETTINGS_READ, endpoint, lambda: self._client.get(endpoint), dict)

    def spending_account_ids(self) -> set[str]:
        return {str(value) for value in self.settings().get("accountIds") or []}

    def report(self, window: dict[str, str]) -> dict[str, Any]:
        endpoint = "/spending/report"
        return self._read(
            CAP_REPORT_READ, endpoint, lambda: self._client.post(endpoint, window), dict
        )

    def uncategorized_count(self, window: dict[str, str]) -> int:
        endpoint = "/spending/cash-activities/search"
        payload = {"status": "uncategorized", **window, "limit": 1, "offset": 0}
        result = self._read(
            CAP_SEARCH_READ, endpoint, lambda: self._client.post(endpoint, payload), dict
        )
        return int(result.get("totalCount") or 0)

    def rules(self) -> list[dict[str, Any]]:
        endpoint = "/spending/rules"
        rows = self._read(CAP_RULE_READ, endpoint, lambda: self._client.get(endpoint), list)
        problem = rule_list_problem(rows)
        if problem:
            self._incompatible(CAP_RULE_READ, endpoint, problem)
        return rows

    def _budget_endpoint(self, suffix: str = "", period_key: str | None = None) -> str:
        endpoint = f"/spending/budget{suffix}"
        checked = validate_period_key(period_key)
        if checked is None:
            return endpoint
        return f"{endpoint}?{urlencode({'periodKey': checked})}"

    def budget(self, period_key: str | None = None) -> dict[str, Any]:
        """Read the budget document without asserting its full shape.

        This is the capability probe: it answers "does this build serve
        ``GET /spending/budget``". Use :meth:`budget_snapshot` when the caller
        needs a structurally valid ``BudgetSnapshot`` to reason about.
        """
        endpoint = self._budget_endpoint(period_key=period_key)
        return self._read(CAP_BUDGET_READ, endpoint, lambda: self._client.get(endpoint), dict)

    def budget_snapshot(self, period_key: str | None = None) -> dict[str, Any]:
        """Read a strictly validated ``BudgetSnapshot`` for one period."""
        endpoint = self._budget_endpoint(period_key=period_key)
        payload = self._read(CAP_BUDGET_READ, endpoint, lambda: self._client.get(endpoint), dict)
        return self._validate_budget_snapshot(payload, CAP_BUDGET_READ, endpoint)

    def assignment_rows(self, activity_id: str) -> list[dict[str, Any]]:
        """Return the raw assignment rows for one activity (capability-gated)."""
        endpoint = f"/spending/activities/{_q(activity_id)}/assignments"
        return self._read(CAP_ASSIGNMENT_READ, endpoint, lambda: self._client.get(endpoint), list)

    def assignment_rows_for(
        self, activity_ids: Iterable[str]
    ) -> dict[str, list[dict[str, Any]]]:
        return {activity_id: self.assignment_rows(activity_id) for activity_id in activity_ids}

    # -- writes (activity category assignment) --------------------------------

    def assign(self, activity_id: str, taxonomy_id: str, category_id: str) -> None:
        endpoint = f"/spending/activities/{_q(activity_id)}/assignments"
        self._call(
            CAP_ASSIGNMENT_WRITE,
            endpoint,
            lambda: self._client.put(
                endpoint, {"taxonomyId": taxonomy_id, "categoryId": category_id}
            ),
        )

    def unassign(self, activity_id: str, taxonomy_id: str) -> None:
        endpoint = f"/spending/activities/{_q(activity_id)}/assignments/{_q(taxonomy_id)}"
        self._call(CAP_ASSIGNMENT_DELETE, endpoint, lambda: self._client.delete(endpoint))

    # -- writes (taxonomy / category configuration) ---------------------------

    def create_category(
        self,
        taxonomy_id: str,
        name: str,
        key: str,
        *,
        parent_id: str | None = None,
        color: str = "#808080",
        description: str | None = None,
        sort_order: int = 0,
        icon: str | None = None,
    ) -> dict[str, Any]:
        """Create one category. Mirrors upstream ``NewCategory`` exactly."""
        if not str(name or "").strip() or not str(key or "").strip():
            raise DecisionError("create_category requires a non-empty name and key")
        endpoint = "/taxonomies/categories"
        body = {
            "id": None,
            "taxonomyId": taxonomy_id,
            "parentId": parent_id,
            "name": name,
            "key": key,
            "color": color,
            "description": description,
            "sortOrder": int(sort_order),
            "icon": icon,
        }
        return self._configuration_write(
            CAP_CATEGORY_CREATE,
            endpoint,
            lambda: self._client.post(endpoint, body),
            self._validate_category,
        )

    def update_category(self, category: dict[str, Any]) -> dict[str, Any]:
        """Replace one category with a complete ``Category`` object.

        There is no partial-update route (see ``CAP_CATEGORY_PATCH`` in
        :data:`KNOWN_API_GAPS`), so callers must read the live category, change
        the fields they mean to change, and pass the whole row back.
        """
        if not isinstance(category, dict):
            raise DecisionError("update_category requires a complete category object")
        missing = sorted(
            field
            for field in ("id", "taxonomyId", "name", "key", "color", "createdAt", "updatedAt")
            if not str(category.get(field) or "").strip()
        )
        if not isinstance(category.get("sortOrder"), int) or isinstance(
            category.get("sortOrder"), bool
        ):
            missing.append("sortOrder")
        if missing:
            raise DecisionError(
                "update_category requires a complete category object; missing "
                + ", ".join(sorted(missing))
            )
        endpoint = "/taxonomies/categories"
        body = dict(category)
        return self._configuration_write(
            CAP_CATEGORY_UPDATE,
            endpoint,
            lambda: self._client.put(endpoint, body),
            self._validate_category,
        )

    def move_category(
        self,
        taxonomy_id: str,
        category_id: str,
        new_parent_id: str | None,
        position: int,
    ) -> dict[str, Any]:
        """Reparent and/or reorder one category."""
        if new_parent_id is not None and new_parent_id == category_id:
            raise DecisionError("a category cannot be its own parent")
        endpoint = "/taxonomies/categories/move"
        body = {
            "taxonomyId": taxonomy_id,
            "categoryId": category_id,
            "newParentId": new_parent_id,
            "position": int(position),
        }
        return self._configuration_write(
            CAP_CATEGORY_MOVE,
            endpoint,
            lambda: self._client.post(endpoint, body),
            self._validate_category,
        )

    def delete_category(self, taxonomy_id: str, category_id: str) -> None:
        """Delete one leaf category.

        Wealthfolio refuses to delete a category that still has children, asset
        assignments, spending references, or allocation targets, and returns
        that refusal as an HTTP error rather than silently cascading.
        """
        endpoint = f"/taxonomies/{_q(taxonomy_id)}/categories/{_q(category_id)}"
        self._configuration_write(
            CAP_CATEGORY_DELETE, endpoint, lambda: self._client.delete(endpoint)
        )

    # -- writes (categorization rules) ----------------------------------------

    def create_rule(self, rule: dict[str, Any]) -> dict[str, Any]:
        """Create one categorization rule (upstream ``NewCategorizationRule``)."""
        endpoint = "/spending/rules"
        body = self._rule_body(rule)
        return self._configuration_write(
            CAP_RULE_WRITE,
            endpoint,
            lambda: self._client.post(endpoint, body),
            self._validate_rule,
        )

    def update_rule(self, rule_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        """Patch one categorization rule (upstream ``UpdateCategorizationRule``)."""
        if not isinstance(patch, dict) or not patch:
            raise DecisionError("update_rule requires a non-empty patch object")
        allowed = {
            "name",
            "pattern",
            "matchType",
            "taxonomyId",
            "categoryId",
            "activityType",
            "priority",
            "isGlobal",
            "accountId",
        }
        unknown = sorted(set(patch) - allowed)
        if unknown:
            raise DecisionError(f"unknown categorization-rule fields: {', '.join(unknown)}")
        if "matchType" in patch and patch["matchType"] not in RULE_MATCH_TYPES:
            raise DecisionError(f"unsupported rule matchType: {patch['matchType']!r}")
        endpoint = f"/spending/rules/{_q(rule_id)}"
        body = dict(patch)
        return self._configuration_write(
            CAP_RULE_UPDATE,
            endpoint,
            lambda: self._client.put(endpoint, body),
            self._validate_rule,
        )

    def delete_rule(self, rule_id: str) -> None:
        endpoint = f"/spending/rules/{_q(rule_id)}"
        self._configuration_write(
            CAP_RULE_DELETE, endpoint, lambda: self._client.delete(endpoint)
        )

    def rerun_rules(self, *, only_uncategorized: bool = True) -> int:
        """Re-apply every rule; returns how many activities were categorized."""
        endpoint = "/spending/rules/rerun"
        body = {"onlyUncategorized": bool(only_uncategorized)}
        result = self._configuration_write(
            CAP_RULE_RERUN, endpoint, lambda: self._client.post(endpoint, body)
        )
        if result is None:
            return 0
        if isinstance(result, bool) or not isinstance(result, int):
            self._incompatible(CAP_RULE_RERUN, endpoint, "rerun response is not an integer count")
        return int(result)

    @staticmethod
    def _rule_body(rule: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(rule, dict):
            raise DecisionError("create_rule requires a rule object")
        for field in ("name", "pattern"):
            if not isinstance(rule.get(field), str) or not rule[field].strip():
                raise DecisionError(f"categorization rule requires a non-empty {field}")
        match_type = rule.get("matchType", "contains")
        if match_type not in RULE_MATCH_TYPES:
            raise DecisionError(f"unsupported rule matchType: {match_type!r}")
        return {
            "id": rule.get("id"),
            "name": rule["name"],
            "pattern": rule["pattern"],
            "matchType": match_type,
            "taxonomyId": rule.get("taxonomyId"),
            "categoryId": rule.get("categoryId"),
            "activityType": rule.get("activityType"),
            "priority": int(rule.get("priority") or 0),
            "isGlobal": bool(rule.get("isGlobal", True)),
            "accountId": rule.get("accountId"),
        }

    # -- writes (budget configuration) ----------------------------------------

    def upsert_budget_target(
        self, target: dict[str, Any], *, period_key: str | None = None
    ) -> dict[str, Any]:
        """Create or replace one budget target; returns the whole snapshot."""
        body = self._budget_target_body(target)
        endpoint = self._budget_endpoint("/targets", period_key)
        return self._configuration_write(
            CAP_BUDGET_TARGET_WRITE,
            endpoint,
            lambda: self._client.post(endpoint, body),
            self._validate_budget_snapshot,
        )

    def delete_budget_target(
        self, target_id: str, *, period_key: str | None = None
    ) -> dict[str, Any]:
        endpoint = self._budget_endpoint(f"/targets/{_q(target_id)}", period_key)
        return self._configuration_write(
            CAP_BUDGET_TARGET_DELETE,
            endpoint,
            lambda: self._client.delete(endpoint),
            self._validate_budget_snapshot,
        )

    def upsert_budget_rollover(
        self, setting: dict[str, Any], *, period_key: str | None = None
    ) -> dict[str, Any]:
        body = self._budget_rollover_body(setting)
        endpoint = self._budget_endpoint("/rollovers", period_key)
        return self._configuration_write(
            CAP_BUDGET_ROLLOVER_WRITE,
            endpoint,
            lambda: self._client.post(endpoint, body),
            self._validate_budget_snapshot,
        )

    def delete_budget_rollover(
        self, setting_id: str, *, period_key: str | None = None
    ) -> dict[str, Any]:
        endpoint = self._budget_endpoint(f"/rollovers/{_q(setting_id)}", period_key)
        return self._configuration_write(
            CAP_BUDGET_ROLLOVER_DELETE,
            endpoint,
            lambda: self._client.delete(endpoint),
            self._validate_budget_snapshot,
        )

    def create_budget_group(
        self, group: dict[str, Any], *, period_key: str | None = None
    ) -> dict[str, Any]:
        if not isinstance(group, dict) or not str(group.get("name") or "").strip():
            raise DecisionError("budget group requires a non-empty name")
        body = {
            "id": group.get("id"),
            "name": group["name"],
            "key": group.get("key"),
            "color": group.get("color"),
            "icon": group.get("icon"),
            "sortOrder": group.get("sortOrder"),
            "isSystem": bool(group.get("isSystem", False)),
        }
        endpoint = self._budget_endpoint("/groups", period_key)
        return self._configuration_write(
            CAP_BUDGET_GROUP_CREATE,
            endpoint,
            lambda: self._client.post(endpoint, body),
            self._validate_budget_snapshot,
        )

    def update_budget_group(
        self, group_id: str, patch: dict[str, Any], *, period_key: str | None = None
    ) -> dict[str, Any]:
        if not isinstance(patch, dict) or not patch:
            raise DecisionError("update_budget_group requires a non-empty patch object")
        unknown = sorted(set(patch) - {"name", "color", "icon", "sortOrder"})
        if unknown:
            raise DecisionError(f"unknown budget group fields: {', '.join(unknown)}")
        endpoint = self._budget_endpoint(f"/groups/{_q(group_id)}", period_key)
        body = dict(patch)
        return self._configuration_write(
            CAP_BUDGET_GROUP_UPDATE,
            endpoint,
            lambda: self._client.put(endpoint, body),
            self._validate_budget_snapshot,
        )

    def delete_budget_group(
        self,
        group_id: str,
        reassign_to_group_id: str,
        *,
        period_key: str | None = None,
    ) -> dict[str, Any]:
        """Delete a group, moving its categories to ``reassign_to_group_id``.

        Wealthfolio requires the reassignment target as a JSON body on the
        DELETE, exactly as its own web client sends it, so this uses the
        client's body-carrying delete.
        """
        if not str(reassign_to_group_id or "").strip():
            raise DecisionError("delete_budget_group requires a reassignment group id")
        endpoint = self._budget_endpoint(f"/groups/{_q(group_id)}", period_key)
        body = {"reassignToGroupId": reassign_to_group_id}
        return self._configuration_write(
            CAP_BUDGET_GROUP_DELETE,
            endpoint,
            lambda: self._client.delete(endpoint, body),
            self._validate_budget_snapshot,
        )

    def assign_category_to_budget_group(
        self,
        category_id: str,
        group_id: str,
        *,
        taxonomy_id: str = SPENDING_TAXONOMY,
        period_key: str | None = None,
    ) -> dict[str, Any]:
        if not str(category_id or "").strip() or not str(group_id or "").strip():
            raise DecisionError("budget group assignment requires a category and a group")
        body = {"categoryId": category_id, "groupId": group_id, "taxonomyId": taxonomy_id}
        endpoint = self._budget_endpoint("/group-assignments", period_key)
        return self._configuration_write(
            CAP_BUDGET_GROUP_ASSIGN,
            endpoint,
            lambda: self._client.post(endpoint, body),
            self._validate_budget_snapshot,
        )

    def copy_budget_targets(
        self, source_period_key: str, target_period_key: str, *, overwrite: bool = False
    ) -> dict[str, Any]:
        source = validate_period_key(source_period_key)
        destination = validate_period_key(target_period_key)
        if source is None or destination is None:
            raise DecisionError("copy_budget_targets requires explicit source and target periods")
        body = {
            "sourcePeriodKey": source,
            "targetPeriodKey": destination,
            "overwrite": bool(overwrite),
        }
        endpoint = "/spending/budget/copy"
        return self._configuration_write(
            CAP_BUDGET_COPY,
            endpoint,
            lambda: self._client.post(endpoint, body),
            self._validate_budget_snapshot,
        )

    @staticmethod
    def _budget_target_body(target: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(target, dict):
            raise DecisionError("budget target must be an object")
        target_type = target.get("targetType")
        if target_type not in BUDGET_TARGET_TYPES:
            raise DecisionError(f"unsupported budget target type: {target_type!r}")
        amount = target.get("amount")
        if not isinstance(amount, str) or not amount.strip():
            raise DecisionError("budget target amount must be a decimal string")
        period_key = validate_period_key(target.get("periodKey"))
        if period_key is None:
            raise DecisionError("budget target requires an explicit periodKey")
        body = {
            "id": target.get("id"),
            "periodKey": period_key,
            "targetType": target_type,
            "taxonomyId": target.get("taxonomyId"),
            "categoryId": target.get("categoryId"),
            "groupId": target.get("groupId"),
            "amount": amount,
        }
        if target_type == "category":
            if not body["taxonomyId"] or not body["categoryId"] or body["groupId"]:
                raise DecisionError(
                    "a category budget target requires taxonomyId and categoryId only"
                )
        elif not body["groupId"] or body["taxonomyId"] or body["categoryId"]:
            raise DecisionError("a group_buffer budget target requires groupId only")
        return body

    @staticmethod
    def _budget_rollover_body(setting: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(setting, dict):
            raise DecisionError("budget rollover setting must be an object")
        target_type = setting.get("targetType")
        if target_type not in BUDGET_ROLLOVER_TARGET_TYPES:
            raise DecisionError(f"unsupported rollover target type: {target_type!r}")
        start_month = validate_period_key(setting.get("startMonth"))
        if start_month is None or start_month == DEFAULT_PERIOD_KEY:
            raise DecisionError("budget rollover requires a YYYY-MM startMonth")
        starting_balance = setting.get("startingBalance", "0")
        if not isinstance(starting_balance, str) or not starting_balance.strip():
            raise DecisionError("budget rollover startingBalance must be a decimal string")
        body = {
            "id": setting.get("id"),
            "targetType": target_type,
            "taxonomyId": setting.get("taxonomyId"),
            "categoryId": setting.get("categoryId"),
            "groupId": setting.get("groupId"),
            "enabled": bool(setting.get("enabled", True)),
            "startMonth": start_month,
            "startingBalance": starting_balance,
        }
        if target_type == "category":
            if not body["taxonomyId"] or not body["categoryId"] or body["groupId"]:
                raise DecisionError("a category rollover requires taxonomyId and categoryId only")
        elif not body["groupId"] or body["taxonomyId"] or body["categoryId"]:
            raise DecisionError("a group rollover requires groupId only")
        return body

    # -- backup ---------------------------------------------------------------

    def backup(self) -> Any:
        """Request a database backup, translating an unsupported endpoint only.

        A supported call that simply did not confirm a backup (a falsy but
        exception-free result) is returned unchanged; the caller decides
        whether that is fatal, exactly as before this adapter existed.
        """
        endpoint = "/utilities/database/backup"
        return self._call(CAP_BACKUP, endpoint, lambda: self._client.backup_database())

    def backups(self) -> list[dict[str, Any]]:
        """List backup metadata through Wealthfolio's supported REST endpoint."""
        endpoint = "/utilities/database/backups"
        return self._read(CAP_BACKUP_READ, endpoint, lambda: self._client.get(endpoint), list)
