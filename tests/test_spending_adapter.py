"""Contract tests for the capability-gated Wealthfolio Spending adapter.

Every test drives `SpendingAdapter` through a synthetic in-memory fake of
`WealthfolioClient` -- never a live HTTP connection and never SQLite -- so
these tests double as executable documentation of exactly which Spending
capabilities this repository depends on, and what happens when Wealthfolio
does not support one of them.
"""

from __future__ import annotations

import pytest

from importers.monarch.wealthfolio_client import WealthfolioError
from importers.rebuild.decisions import DecisionError
from importers.simplefin.spending_adapter import (
    CAP_ASSIGNMENT_DELETE,
    CAP_ASSIGNMENT_READ,
    CAP_ASSIGNMENT_WRITE,
    CAP_BACKUP,
    CAP_BACKUP_READ,
    CAP_BUDGET_COPY,
    CAP_BUDGET_GROUP_ASSIGN,
    CAP_BUDGET_GROUP_CREATE,
    CAP_BUDGET_GROUP_DELETE,
    CAP_BUDGET_GROUP_UPDATE,
    CAP_BUDGET_PERIOD_LIST,
    CAP_BUDGET_READ,
    CAP_BUDGET_ROLLOVER_DELETE,
    CAP_BUDGET_ROLLOVER_WRITE,
    CAP_BUDGET_TARGET_DELETE,
    CAP_BUDGET_TARGET_WRITE,
    CAP_CATEGORY_CREATE,
    CAP_CATEGORY_DELETE,
    CAP_CATEGORY_MOVE,
    CAP_CATEGORY_PATCH,
    CAP_CATEGORY_READ_ONE,
    CAP_CATEGORY_UPDATE,
    CAP_REPORT_READ,
    CAP_RULE_DELETE,
    CAP_RULE_READ,
    CAP_RULE_RERUN,
    CAP_RULE_UPDATE,
    CAP_RULE_WRITE,
    CAP_SEARCH_READ,
    CAP_SETTINGS_READ,
    CAP_TAXONOMY_LIST,
    CAP_TAXONOMY_MERGE_IMPORT,
    CAP_TAXONOMY_READ,
    INCOME_TAXONOMY,
    KNOWN_API_GAPS,
    SPENDING_TAXONOMY,
    SUPPORTED_ENDPOINTS,
    CapabilityStatus,
    SpendingAdapter,
    SpendingCapabilityBlocked,
    validate_period_key,
)


class FakeWealthfolioClient:
    """A synthetic, fully in-memory stand-in for `WealthfolioClient`.

    Configure canned responses/errors per path; every call is recorded in
    `.calls` so tests can assert exactly what was (or was not) attempted.
    """

    def __init__(self):
        self.get_responses: dict[str, object] = {}
        self.get_errors: dict[str, Exception] = {}
        self.post_responses: dict[str, object] = {}
        self.post_errors: dict[str, Exception] = {}
        self.put_responses: dict[str, object] = {}
        self.put_errors: dict[str, Exception] = {}
        self.delete_responses: dict[str, object] = {}
        self.delete_errors: dict[str, Exception] = {}
        self.backup_response: object = {"filename": "synthetic-backup.db"}
        self.backup_error: Exception | None = None
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[tuple[str, str, object]] = []

    def get(self, path):
        self.calls.append(("GET", path))
        if path in self.get_errors:
            raise self.get_errors[path]
        return self.get_responses.get(path)

    def post(self, path, payload):
        self.calls.append(("POST", path))
        self.bodies.append(("POST", path, payload))
        if path in self.post_errors:
            raise self.post_errors[path]
        return self.post_responses.get(path)

    def put(self, path, payload):
        self.calls.append(("PUT", path))
        self.bodies.append(("PUT", path, payload))
        if path in self.put_errors:
            raise self.put_errors[path]
        return self.put_responses.get(path)

    def delete(self, path, payload=None):
        self.calls.append(("DELETE", path))
        self.bodies.append(("DELETE", path, payload))
        if path in self.delete_errors:
            raise self.delete_errors[path]
        return self.delete_responses.get(path)

    def backup_database(self):
        self.calls.append(("BACKUP", ""))
        if self.backup_error:
            raise self.backup_error
        return self.backup_response


WINDOW = {"startDate": "2024-01-01T00:00:00Z", "endDate": "2024-01-31T23:59:59Z"}

SYNTHETIC_CATEGORY = {
    "id": "cat-1",
    "taxonomyId": SPENDING_TAXONOMY,
    "parentId": None,
    "name": "Food",
    "key": "food",
    "color": "#808080",
    "description": None,
    "sortOrder": 0,
    "icon": None,
    "createdAt": "2024-01-01T00:00:00",
    "updatedAt": "2024-01-01T00:00:00",
}

SYNTHETIC_RULE = {
    "id": "rule-1",
    "name": "Synthetic rule",
    "pattern": "SYNTHETIC PAYEE",
    "matchType": "contains",
    "taxonomyId": SPENDING_TAXONOMY,
    "categoryId": "cat-1",
    "activityType": None,
    "priority": 0,
    "isGlobal": True,
    "accountId": None,
}

SYNTHETIC_SNAPSHOT = {
    "state": {
        "groups": [],
        "groupAssignments": [],
        "targets": [],
        "rolloverSettings": [],
    },
    "computed": {
        "currency": "USD",
        "periodKey": "2024-01",
        "fxAsOf": None,
        "groupRows": [],
        "ungroupedRows": [],
        "incomeRows": [],
        "totals": {},
    },
}


def unlocked(client) -> SpendingAdapter:
    """An adapter with configuration writes deliberately enabled."""
    return SpendingAdapter(client, allow_configuration_writes=True)


def test_capability_status_available_property():
    status = CapabilityStatus("x", "available", "/x")
    assert status.available
    assert not CapabilityStatus("x", "unsupported", "/x").available


def test_capability_status_explanation_prefers_known_gap():
    status = CapabilityStatus(
        CAP_CATEGORY_PATCH, "unsupported", "(no route)", "generic detail"
    )
    assert status.explanation == KNOWN_API_GAPS[CAP_CATEGORY_PATCH]
    other = CapabilityStatus("something-else", "error", "/x", "boom")
    assert other.explanation == "boom"


def test_spending_capability_blocked_message_is_actionable():
    status = CapabilityStatus(CAP_CATEGORY_PATCH, "unsupported", "(no route)")
    exc = SpendingCapabilityBlocked(status)
    assert isinstance(exc, DecisionError)
    assert "taxonomyCategoryPatch" in str(exc)
    assert "unsupported" in str(exc)
    assert KNOWN_API_GAPS[CAP_CATEGORY_PATCH] in str(exc)
    assert exc.status is status


def test_rule_and_budget_writes_are_not_declared_as_api_gaps():
    """Wealthfolio 3.7.0 exposes full rule and budget CRUD.

    An earlier revision of this adapter recorded them as permanent API gaps.
    That was factually wrong, and this test keeps it from coming back.
    """
    for capability in (
        CAP_RULE_WRITE,
        CAP_RULE_UPDATE,
        CAP_RULE_DELETE,
        CAP_RULE_RERUN,
        CAP_BUDGET_TARGET_WRITE,
        CAP_BUDGET_TARGET_DELETE,
        CAP_BUDGET_ROLLOVER_WRITE,
        CAP_BUDGET_GROUP_CREATE,
        CAP_CATEGORY_CREATE,
        CAP_CATEGORY_UPDATE,
        CAP_CATEGORY_MOVE,
        CAP_CATEGORY_DELETE,
    ):
        assert capability not in KNOWN_API_GAPS, capability
        assert capability in SUPPORTED_ENDPOINTS, capability


def test_known_api_gaps_have_no_supported_endpoint():
    for capability in KNOWN_API_GAPS:
        assert capability not in SUPPORTED_ENDPOINTS, capability


def test_taxonomy_success_returns_categories_and_makes_one_call():
    client = FakeWealthfolioClient()
    client.get_responses[f"/taxonomies/{SPENDING_TAXONOMY}"] = {
        "categories": [{"id": "groceries"}]
    }
    adapter = SpendingAdapter(client)
    categories = adapter.taxonomy(SPENDING_TAXONOMY)
    assert categories == [{"id": "groceries"}]
    assert client.calls == [("GET", f"/taxonomies/{SPENDING_TAXONOMY}")]
    status = adapter.status(f"{CAP_TAXONOMY_READ}:{SPENDING_TAXONOMY}")
    assert status is not None and status.available


def test_taxonomy_unsupported_404_raises_blocked():
    client = FakeWealthfolioClient()
    path = f"/taxonomies/{SPENDING_TAXONOMY}"
    client.get_errors[path] = WealthfolioError(404, path, "not found")
    adapter = SpendingAdapter(client)
    with pytest.raises(SpendingCapabilityBlocked) as exc_info:
        adapter.taxonomy(SPENDING_TAXONOMY)
    assert exc_info.value.status.status == "unsupported"
    assert exc_info.value.status.endpoint == path


def test_taxonomy_incompatible_shape_raises_blocked():
    client = FakeWealthfolioClient()
    path = f"/taxonomies/{SPENDING_TAXONOMY}"
    client.get_responses[path] = {"categories": "not-a-list"}
    adapter = SpendingAdapter(client)
    with pytest.raises(SpendingCapabilityBlocked) as exc_info:
        adapter.taxonomy(SPENDING_TAXONOMY)
    assert exc_info.value.status.status == "incompatible"


def test_taxonomy_server_error_is_classified_as_error_not_unsupported():
    client = FakeWealthfolioClient()
    path = f"/taxonomies/{SPENDING_TAXONOMY}"
    client.get_errors[path] = WealthfolioError(500, path, "boom")
    adapter = SpendingAdapter(client)
    with pytest.raises(SpendingCapabilityBlocked) as exc_info:
        adapter.taxonomy(SPENDING_TAXONOMY)
    assert exc_info.value.status.status == "error"


def test_catalogs_returns_both_taxonomies():
    client = FakeWealthfolioClient()
    client.get_responses[f"/taxonomies/{SPENDING_TAXONOMY}"] = {"categories": [{"id": "s"}]}
    client.get_responses[f"/taxonomies/{INCOME_TAXONOMY}"] = {"categories": [{"id": "i"}]}
    adapter = SpendingAdapter(client)
    catalogs = adapter.catalogs()
    assert catalogs == {
        SPENDING_TAXONOMY: [{"id": "s"}],
        INCOME_TAXONOMY: [{"id": "i"}],
    }


def test_settings_and_spending_account_ids():
    client = FakeWealthfolioClient()
    client.get_responses["/spending/settings"] = {
        "enabled": True,
        "accountIds": ["acct-1", "acct-2"],
    }
    adapter = SpendingAdapter(client)
    assert adapter.settings()["enabled"] is True
    assert adapter.spending_account_ids() == {"acct-1", "acct-2"}


def test_settings_unsupported_raises_blocked():
    client = FakeWealthfolioClient()
    client.get_errors["/spending/settings"] = WealthfolioError(404, "/spending/settings", "no")
    adapter = SpendingAdapter(client)
    with pytest.raises(SpendingCapabilityBlocked) as exc_info:
        adapter.settings()
    assert exc_info.value.status.capability == CAP_SETTINGS_READ


def test_report_and_uncategorized_count():
    client = FakeWealthfolioClient()
    client.post_responses["/spending/report"] = {"current": {"income": "10", "outflow": "5"}}
    client.post_responses["/spending/cash-activities/search"] = {"totalCount": 3}
    adapter = SpendingAdapter(client)
    assert adapter.report(WINDOW) == {"current": {"income": "10", "outflow": "5"}}
    assert adapter.uncategorized_count(WINDOW) == 3


def test_report_unsupported_raises_blocked():
    client = FakeWealthfolioClient()
    client.post_errors["/spending/report"] = WealthfolioError(405, "/spending/report", "no")
    adapter = SpendingAdapter(client)
    with pytest.raises(SpendingCapabilityBlocked) as exc_info:
        adapter.report(WINDOW)
    assert exc_info.value.status.capability == CAP_REPORT_READ
    assert exc_info.value.status.status == "unsupported"


def test_assignment_rows_translates_404_but_not_other_exceptions():
    blocked_client = FakeWealthfolioClient()
    endpoint = "/spending/activities/a1/assignments"
    blocked_client.get_errors[endpoint] = WealthfolioError(404, endpoint, "no")
    adapter = SpendingAdapter(blocked_client)
    with pytest.raises(SpendingCapabilityBlocked) as exc_info:
        adapter.assignment_rows("a1")
    assert exc_info.value.status.capability == CAP_ASSIGNMENT_READ

    broken_client = FakeWealthfolioClient()
    broken_client.get_errors[endpoint] = RuntimeError("synthetic failure")
    adapter = SpendingAdapter(broken_client)
    with pytest.raises(RuntimeError, match="synthetic failure"):
        adapter.assignment_rows("a1")


def test_assignment_rows_defaults_missing_response_to_empty_list():
    client = FakeWealthfolioClient()
    adapter = SpendingAdapter(client)
    assert adapter.assignment_rows("a1") == []


def test_assignment_rows_for_batches_multiple_activities():
    client = FakeWealthfolioClient()
    client.get_responses["/spending/activities/a1/assignments"] = [
        {"taxonomyId": SPENDING_TAXONOMY, "categoryId": "groceries"}
    ]
    client.get_responses["/spending/activities/a2/assignments"] = []
    adapter = SpendingAdapter(client)
    rows = adapter.assignment_rows_for(["a1", "a2"])
    assert rows["a1"] == [{"taxonomyId": SPENDING_TAXONOMY, "categoryId": "groceries"}]
    assert rows["a2"] == []


def test_assign_translates_404_to_blocked():
    client = FakeWealthfolioClient()
    endpoint = "/spending/activities/a1/assignments"
    client.put_errors[endpoint] = WealthfolioError(404, endpoint, "no")
    adapter = SpendingAdapter(client)
    with pytest.raises(SpendingCapabilityBlocked) as exc_info:
        adapter.assign("a1", SPENDING_TAXONOMY, "groceries")
    assert exc_info.value.status.capability == CAP_ASSIGNMENT_WRITE


def test_assign_lets_generic_exception_propagate_unchanged():
    """A synthetic non-HTTP failure (as tests use to exercise rollback) must
    never be swallowed or reclassified -- only real WealthfolioError 404/405
    responses become SpendingCapabilityBlocked."""
    client = FakeWealthfolioClient()
    endpoint = "/spending/activities/a1/assignments"
    client.put_errors[endpoint] = RuntimeError("synthetic failure")
    adapter = SpendingAdapter(client)
    with pytest.raises(RuntimeError, match="synthetic failure"):
        adapter.assign("a1", SPENDING_TAXONOMY, "groceries")


def test_unassign_translates_405_to_blocked():
    client = FakeWealthfolioClient()
    endpoint = f"/spending/activities/a1/assignments/{SPENDING_TAXONOMY}"
    client.delete_errors[endpoint] = WealthfolioError(405, endpoint, "no")
    adapter = SpendingAdapter(client)
    with pytest.raises(SpendingCapabilityBlocked) as exc_info:
        adapter.unassign("a1", SPENDING_TAXONOMY)
    assert exc_info.value.status.capability == CAP_ASSIGNMENT_DELETE


def test_backup_unsupported_raises_blocked():
    client = FakeWealthfolioClient()
    client.backup_error = WealthfolioError(404, "/utilities/database/backup", "no")
    adapter = SpendingAdapter(client)
    with pytest.raises(SpendingCapabilityBlocked) as exc_info:
        adapter.backup()
    assert exc_info.value.status.capability == CAP_BACKUP


def test_backup_falsy_without_exception_is_returned_unchanged():
    """An unconfirmed backup (no exception, just a falsy result) is a
    different, pre-existing failure mode than an unsupported endpoint, and
    must not be translated into SpendingCapabilityBlocked -- the caller
    still decides that a falsy backup means "not confirmed"."""
    client = FakeWealthfolioClient()
    client.backup_response = None
    adapter = SpendingAdapter(client)
    assert adapter.backup() is None


def test_backup_listing_uses_supported_rest_endpoint():
    client = FakeWealthfolioClient()
    client.get_responses["/utilities/database/backups"] = [{
        "filename": "synthetic-backup.db",
        "sizeBytes": 128,
        "modifiedAt": "2026-08-28T00:00:00Z",
    }]
    adapter = SpendingAdapter(client)

    assert adapter.backups()[0]["sizeBytes"] == 128
    assert adapter.status(CAP_BACKUP_READ).available


def test_capabilities_snapshot_covers_every_read_safe_capability():
    client = FakeWealthfolioClient()
    client.get_responses["/taxonomies"] = []
    client.get_responses[f"/taxonomies/{SPENDING_TAXONOMY}"] = {"categories": []}
    client.get_responses[f"/taxonomies/{INCOME_TAXONOMY}"] = {"categories": []}
    client.get_responses["/spending/rules"] = []
    client.get_responses["/spending/budget"] = {}
    client.post_responses["/spending/report"] = {"current": {}}
    client.post_responses["/spending/cash-activities/search"] = {"totalCount": 0}
    adapter = SpendingAdapter(client)
    snapshot = adapter.capabilities()
    for capability in (
        CAP_SETTINGS_READ,
        CAP_REPORT_READ,
        CAP_SEARCH_READ,
        CAP_RULE_READ,
        CAP_BUDGET_READ,
        CAP_TAXONOMY_LIST,
        f"{CAP_TAXONOMY_READ}:{SPENDING_TAXONOMY}",
        f"{CAP_TAXONOMY_READ}:{INCOME_TAXONOMY}",
    ):
        assert snapshot[capability].available, capability
    # Genuine, documented gaps: recorded without ever touching the network.
    for capability in (
        CAP_TAXONOMY_MERGE_IMPORT,
        CAP_CATEGORY_PATCH,
        CAP_CATEGORY_READ_ONE,
        CAP_BUDGET_PERIOD_LIST,
    ):
        assert snapshot[capability].status == "unsupported", capability
        assert snapshot[capability].endpoint == "(no route)"
    # Write capabilities that need a real object are never eagerly probed.
    assert CAP_ASSIGNMENT_WRITE not in snapshot
    assert CAP_ASSIGNMENT_DELETE not in snapshot
    assert CAP_BACKUP not in snapshot
    assert CAP_CATEGORY_CREATE not in snapshot
    assert CAP_RULE_WRITE not in snapshot
    assert CAP_BUDGET_TARGET_WRITE not in snapshot


def test_capabilities_records_unsupported_endpoints_without_raising():
    client = FakeWealthfolioClient()
    client.get_errors["/spending/rules"] = WealthfolioError(404, "/spending/rules", "no")
    adapter = SpendingAdapter(client)
    snapshot = adapter.capabilities()
    assert snapshot[CAP_RULE_READ].status == "unsupported"


def test_require_raises_for_a_capability_that_is_never_eagerly_probed():
    client = FakeWealthfolioClient()
    adapter = SpendingAdapter(client)
    with pytest.raises(SpendingCapabilityBlocked) as exc_info:
        adapter.require(CAP_ASSIGNMENT_WRITE, endpoint="/spending/activities/a1/assignments")
    assert exc_info.value.status.capability == CAP_ASSIGNMENT_WRITE
    assert exc_info.value.status.status == "unsupported"


def test_require_succeeds_once_a_capability_is_used_successfully():
    client = FakeWealthfolioClient()
    client.get_responses["/spending/settings"] = {"enabled": True, "accountIds": []}
    adapter = SpendingAdapter(client)
    adapter.settings()
    status = adapter.require(CAP_SETTINGS_READ)
    assert status.available


# -- configuration writes are locked off by default ---------------------------


LOCKED_OPERATIONS = (
    ("create_category", lambda a: a.create_category(SPENDING_TAXONOMY, "Food", "food")),
    ("update_category", lambda a: a.update_category(dict(SYNTHETIC_CATEGORY))),
    ("move_category", lambda a: a.move_category(SPENDING_TAXONOMY, "cat-2", "cat-1", 0)),
    ("delete_category", lambda a: a.delete_category(SPENDING_TAXONOMY, "cat-1")),
    ("create_rule", lambda a: a.create_rule(dict(SYNTHETIC_RULE))),
    ("update_rule", lambda a: a.update_rule("rule-1", {"priority": 5})),
    ("delete_rule", lambda a: a.delete_rule("rule-1")),
    ("rerun_rules", lambda a: a.rerun_rules()),
    (
        "upsert_budget_target",
        lambda a: a.upsert_budget_target(
            {
                "periodKey": "2024-01",
                "targetType": "category",
                "taxonomyId": SPENDING_TAXONOMY,
                "categoryId": "cat-1",
                "amount": "100.00",
            }
        ),
    ),
    ("delete_budget_target", lambda a: a.delete_budget_target("target-1")),
    (
        "upsert_budget_rollover",
        lambda a: a.upsert_budget_rollover(
            {
                "targetType": "category",
                "taxonomyId": SPENDING_TAXONOMY,
                "categoryId": "cat-1",
                "startMonth": "2024-01",
                "startingBalance": "0",
            }
        ),
    ),
    ("delete_budget_rollover", lambda a: a.delete_budget_rollover("rollover-1")),
    ("create_budget_group", lambda a: a.create_budget_group({"name": "Essentials"})),
    ("update_budget_group", lambda a: a.update_budget_group("g1", {"name": "Fixed"})),
    ("delete_budget_group", lambda a: a.delete_budget_group("g1", "g2")),
    (
        "assign_category_to_budget_group",
        lambda a: a.assign_category_to_budget_group("cat-1", "g1"),
    ),
    ("copy_budget_targets", lambda a: a.copy_budget_targets("2024-01", "2024-02")),
)


@pytest.mark.parametrize("name,operation", LOCKED_OPERATIONS, ids=[n for n, _ in LOCKED_OPERATIONS])
def test_configuration_writes_are_locked_and_make_zero_network_calls(name, operation):
    """The default adapter cannot reshape a live instance, even by mistake."""
    client = FakeWealthfolioClient()
    adapter = SpendingAdapter(client)
    assert adapter.configuration_writes_allowed is False
    with pytest.raises(SpendingCapabilityBlocked) as exc_info:
        operation(adapter)
    assert exc_info.value.status.status == "locked"
    assert "allow_configuration_writes=True" in exc_info.value.status.detail
    assert client.calls == []


# -- taxonomy and category ----------------------------------------------------


def test_taxonomies_lists_and_rejects_non_objects():
    client = FakeWealthfolioClient()
    client.get_responses["/taxonomies"] = [{"id": SPENDING_TAXONOMY}]
    adapter = SpendingAdapter(client)
    assert adapter.taxonomies() == [{"id": SPENDING_TAXONOMY}]
    assert adapter.status(CAP_TAXONOMY_LIST).available

    broken = FakeWealthfolioClient()
    broken.get_responses["/taxonomies"] = ["not-an-object"]
    with pytest.raises(SpendingCapabilityBlocked) as exc_info:
        SpendingAdapter(broken).taxonomies()
    assert exc_info.value.status.status == "incompatible"


def test_taxonomy_detail_requires_both_taxonomy_and_categories():
    client = FakeWealthfolioClient()
    path = f"/taxonomies/{SPENDING_TAXONOMY}"
    client.get_responses[path] = {
        "taxonomy": {"id": SPENDING_TAXONOMY, "name": "Spending"},
        "categories": [SYNTHETIC_CATEGORY],
    }
    detail = SpendingAdapter(client).taxonomy_detail(SPENDING_TAXONOMY)
    assert detail["taxonomy"]["id"] == SPENDING_TAXONOMY
    assert detail["categories"] == [SYNTHETIC_CATEGORY]


def test_taxonomy_detail_treats_a_null_body_as_incompatible():
    """The server serialises a missing taxonomy as JSON null with HTTP 200."""
    client = FakeWealthfolioClient()
    client.get_responses[f"/taxonomies/{SPENDING_TAXONOMY}"] = None
    with pytest.raises(SpendingCapabilityBlocked) as exc_info:
        SpendingAdapter(client).taxonomy_detail(SPENDING_TAXONOMY)
    assert exc_info.value.status.status == "incompatible"


def test_create_category_posts_the_upstream_new_category_shape():
    client = FakeWealthfolioClient()
    client.post_responses["/taxonomies/categories"] = SYNTHETIC_CATEGORY
    adapter = unlocked(client)
    created = adapter.create_category(
        SPENDING_TAXONOMY, "Food", "food", color="#808080", sort_order=0
    )
    assert created["id"] == "cat-1"
    method, path, body = client.bodies[-1]
    assert (method, path) == ("POST", "/taxonomies/categories")
    assert body == {
        "id": None,
        "taxonomyId": SPENDING_TAXONOMY,
        "parentId": None,
        "name": "Food",
        "key": "food",
        "color": "#808080",
        "description": None,
        "sortOrder": 0,
        "icon": None,
    }


def test_create_category_validates_the_response_strictly():
    client = FakeWealthfolioClient()
    client.post_responses["/taxonomies/categories"] = {"id": "cat-1"}
    with pytest.raises(SpendingCapabilityBlocked) as exc_info:
        unlocked(client).create_category(SPENDING_TAXONOMY, "Food", "food")
    assert exc_info.value.status.status == "incompatible"


def test_create_category_requires_a_name_and_key():
    client = FakeWealthfolioClient()
    with pytest.raises(DecisionError):
        unlocked(client).create_category(SPENDING_TAXONOMY, "  ", "food")
    assert client.calls == []


def test_update_category_requires_a_complete_object():
    client = FakeWealthfolioClient()
    incomplete = {key: value for key, value in SYNTHETIC_CATEGORY.items() if key != "updatedAt"}
    with pytest.raises(DecisionError, match="updatedAt"):
        unlocked(client).update_category(incomplete)
    assert client.calls == []


def test_update_category_sends_the_whole_row():
    client = FakeWealthfolioClient()
    renamed = dict(SYNTHETIC_CATEGORY, name="Food & Drink")
    client.put_responses["/taxonomies/categories"] = renamed
    assert unlocked(client).update_category(renamed)["name"] == "Food & Drink"
    assert client.bodies[-1] == ("PUT", "/taxonomies/categories", renamed)


def test_move_category_rejects_self_parenting():
    client = FakeWealthfolioClient()
    with pytest.raises(DecisionError):
        unlocked(client).move_category(SPENDING_TAXONOMY, "cat-1", "cat-1", 0)
    assert client.calls == []


def test_move_category_posts_the_move_body():
    client = FakeWealthfolioClient()
    child = dict(SYNTHETIC_CATEGORY, id="cat-2", key="groceries", parentId="cat-1")
    client.post_responses["/taxonomies/categories/move"] = child
    moved = unlocked(client).move_category(SPENDING_TAXONOMY, "cat-2", "cat-1", 3)
    assert moved["parentId"] == "cat-1"
    assert client.bodies[-1] == (
        "POST",
        "/taxonomies/categories/move",
        {
            "taxonomyId": SPENDING_TAXONOMY,
            "categoryId": "cat-2",
            "newParentId": "cat-1",
            "position": 3,
        },
    )


def test_delete_category_uses_the_nested_route_and_url_quotes_ids():
    client = FakeWealthfolioClient()
    unlocked(client).delete_category(SPENDING_TAXONOMY, "cat/1")
    assert client.calls == [("DELETE", f"/taxonomies/{SPENDING_TAXONOMY}/categories/cat%2F1")]


def test_delete_category_translates_a_server_refusal():
    client = FakeWealthfolioClient()
    endpoint = f"/taxonomies/{SPENDING_TAXONOMY}/categories/cat-1"
    client.delete_errors[endpoint] = WealthfolioError(400, endpoint, "category has children")
    with pytest.raises(SpendingCapabilityBlocked) as exc_info:
        unlocked(client).delete_category(SPENDING_TAXONOMY, "cat-1")
    assert exc_info.value.status.status == "error"


# -- categorization rules -----------------------------------------------------


def test_create_rule_normalizes_defaults_and_validates_the_response():
    client = FakeWealthfolioClient()
    client.post_responses["/spending/rules"] = SYNTHETIC_RULE
    created = unlocked(client).create_rule(
        {"name": "Synthetic rule", "pattern": "SYNTHETIC PAYEE", "categoryId": "cat-1"}
    )
    assert created["id"] == "rule-1"
    body = client.bodies[-1][2]
    assert body["matchType"] == "contains"
    assert body["priority"] == 0
    assert body["isGlobal"] is True


def test_create_rule_rejects_an_unknown_match_type():
    client = FakeWealthfolioClient()
    with pytest.raises(DecisionError, match="matchType"):
        unlocked(client).create_rule(
            {"name": "n", "pattern": "p", "matchType": "fuzzy"}
        )
    assert client.calls == []


def test_create_rule_requires_a_pattern():
    client = FakeWealthfolioClient()
    with pytest.raises(DecisionError, match="pattern"):
        unlocked(client).create_rule({"name": "n", "pattern": "   "})
    assert client.calls == []


def test_update_rule_rejects_fields_the_server_does_not_accept():
    client = FakeWealthfolioClient()
    with pytest.raises(DecisionError, match="presetId"):
        unlocked(client).update_rule("rule-1", {"presetId": "x"})
    assert client.calls == []


def test_update_rule_sends_only_the_patched_fields():
    client = FakeWealthfolioClient()
    client.put_responses["/spending/rules/rule-1"] = dict(SYNTHETIC_RULE, priority=9)
    updated = unlocked(client).update_rule("rule-1", {"priority": 9})
    assert updated["priority"] == 9
    assert client.bodies[-1] == ("PUT", "/spending/rules/rule-1", {"priority": 9})


def test_delete_rule_tolerates_an_empty_body():
    client = FakeWealthfolioClient()
    unlocked(client).delete_rule("rule-1")
    assert client.calls == [("DELETE", "/spending/rules/rule-1")]


def test_rerun_rules_returns_the_count_and_rejects_a_non_integer():
    client = FakeWealthfolioClient()
    client.post_responses["/spending/rules/rerun"] = 7
    assert unlocked(client).rerun_rules(only_uncategorized=False) == 7
    assert client.bodies[-1][2] == {"onlyUncategorized": False}

    broken = FakeWealthfolioClient()
    broken.post_responses["/spending/rules/rerun"] = "seven"
    with pytest.raises(SpendingCapabilityBlocked) as exc_info:
        unlocked(broken).rerun_rules()
    assert exc_info.value.status.status == "incompatible"


# -- budgets ------------------------------------------------------------------


@pytest.mark.parametrize("period_key", ["2024-01", "default", None])
def test_validate_period_key_accepts_supported_forms(period_key):
    assert validate_period_key(period_key) == period_key


@pytest.mark.parametrize("period_key", ["2024-13", "2024-1", "24-01", "2024/01", "", "current"])
def test_validate_period_key_rejects_everything_else(period_key):
    with pytest.raises(DecisionError):
        validate_period_key(period_key)


def test_budget_snapshot_validates_the_full_document():
    client = FakeWealthfolioClient()
    client.get_responses["/spending/budget?periodKey=2024-01"] = SYNTHETIC_SNAPSHOT
    snapshot = SpendingAdapter(client).budget_snapshot("2024-01")
    assert snapshot["computed"]["periodKey"] == "2024-01"
    assert client.calls == [("GET", "/spending/budget?periodKey=2024-01")]


def test_budget_snapshot_rejects_a_partial_document():
    client = FakeWealthfolioClient()
    client.get_responses["/spending/budget"] = {"state": {}, "computed": {}}
    with pytest.raises(SpendingCapabilityBlocked) as exc_info:
        SpendingAdapter(client).budget_snapshot()
    assert exc_info.value.status.status == "incompatible"


def test_budget_read_rejects_an_invalid_period_before_any_network_call():
    client = FakeWealthfolioClient()
    with pytest.raises(DecisionError):
        SpendingAdapter(client).budget("2024-13")
    assert client.calls == []


def test_upsert_budget_target_posts_a_category_target():
    client = FakeWealthfolioClient()
    endpoint = "/spending/budget/targets?periodKey=2024-01"
    client.post_responses[endpoint] = SYNTHETIC_SNAPSHOT
    snapshot = unlocked(client).upsert_budget_target(
        {
            "periodKey": "2024-01",
            "targetType": "category",
            "taxonomyId": SPENDING_TAXONOMY,
            "categoryId": "cat-1",
            "amount": "250.00",
        },
        period_key="2024-01",
    )
    assert snapshot["computed"]["periodKey"] == "2024-01"
    assert client.bodies[-1][2]["groupId"] is None


def test_upsert_budget_target_enforces_the_servers_own_validation():
    client = FakeWealthfolioClient()
    with pytest.raises(DecisionError, match="category budget target"):
        unlocked(client).upsert_budget_target(
            {
                "periodKey": "2024-01",
                "targetType": "category",
                "taxonomyId": SPENDING_TAXONOMY,
                "categoryId": "cat-1",
                "groupId": "g1",
                "amount": "250.00",
            }
        )
    with pytest.raises(DecisionError, match="group_buffer"):
        unlocked(client).upsert_budget_target(
            {
                "periodKey": "2024-01",
                "targetType": "group_buffer",
                "categoryId": "cat-1",
                "amount": "250.00",
            }
        )
    with pytest.raises(DecisionError, match="decimal string"):
        unlocked(client).upsert_budget_target(
            {
                "periodKey": "2024-01",
                "targetType": "group_buffer",
                "groupId": "g1",
                "amount": 250,
            }
        )
    assert client.calls == []


def test_delete_budget_target_returns_the_refreshed_snapshot():
    client = FakeWealthfolioClient()
    client.delete_responses["/spending/budget/targets/t1"] = SYNTHETIC_SNAPSHOT
    assert unlocked(client).delete_budget_target("t1")["state"]["targets"] == []


def test_upsert_budget_rollover_requires_a_month_start():
    client = FakeWealthfolioClient()
    with pytest.raises(DecisionError, match="startMonth"):
        unlocked(client).upsert_budget_rollover(
            {
                "targetType": "group",
                "groupId": "g1",
                "startMonth": "default",
                "startingBalance": "0",
            }
        )
    assert client.calls == []


def test_upsert_budget_rollover_posts_a_group_rollover():
    client = FakeWealthfolioClient()
    client.post_responses["/spending/budget/rollovers"] = SYNTHETIC_SNAPSHOT
    unlocked(client).upsert_budget_rollover(
        {
            "targetType": "group",
            "groupId": "g1",
            "startMonth": "2024-01",
            "startingBalance": "0",
        }
    )
    body = client.bodies[-1][2]
    assert body["enabled"] is True
    assert body["categoryId"] is None


def test_delete_budget_rollover_returns_the_refreshed_snapshot():
    client = FakeWealthfolioClient()
    client.delete_responses["/spending/budget/rollovers/r1"] = SYNTHETIC_SNAPSHOT
    assert unlocked(client).delete_budget_rollover("r1")["state"]["rolloverSettings"] == []


def test_budget_group_create_update_and_assign():
    client = FakeWealthfolioClient()
    client.post_responses["/spending/budget/groups"] = SYNTHETIC_SNAPSHOT
    client.put_responses["/spending/budget/groups/g1"] = SYNTHETIC_SNAPSHOT
    client.post_responses["/spending/budget/group-assignments"] = SYNTHETIC_SNAPSHOT
    adapter = unlocked(client)
    adapter.create_budget_group({"name": "Essentials"})
    adapter.update_budget_group("g1", {"name": "Fixed costs"})
    adapter.assign_category_to_budget_group("cat-1", "g1")
    assert [call[0] for call in client.calls] == ["POST", "PUT", "POST"]
    assert client.bodies[-1][2] == {
        "categoryId": "cat-1",
        "groupId": "g1",
        "taxonomyId": SPENDING_TAXONOMY,
    }


def test_update_budget_group_rejects_unknown_fields():
    client = FakeWealthfolioClient()
    with pytest.raises(DecisionError, match="isSystem"):
        unlocked(client).update_budget_group("g1", {"isSystem": True})
    assert client.calls == []


def test_delete_budget_group_sends_the_reassignment_body():
    client = FakeWealthfolioClient()
    client.delete_responses["/spending/budget/groups/g1"] = SYNTHETIC_SNAPSHOT
    unlocked(client).delete_budget_group("g1", "g2")
    assert client.bodies[-1] == (
        "DELETE",
        "/spending/budget/groups/g1",
        {"reassignToGroupId": "g2"},
    )


def test_delete_budget_group_requires_a_reassignment_target():
    client = FakeWealthfolioClient()
    with pytest.raises(DecisionError):
        unlocked(client).delete_budget_group("g1", "")
    assert client.calls == []


def test_copy_budget_targets_requires_two_explicit_periods():
    client = FakeWealthfolioClient()
    client.post_responses["/spending/budget/copy"] = SYNTHETIC_SNAPSHOT
    unlocked(client).copy_budget_targets("2024-01", "2024-02", overwrite=True)
    assert client.bodies[-1][2] == {
        "sourcePeriodKey": "2024-01",
        "targetPeriodKey": "2024-02",
        "overwrite": True,
    }
    with pytest.raises(DecisionError):
        unlocked(client).copy_budget_targets("2024-01", "nope")


def test_known_gaps_are_reported_verbatim():
    gaps = SpendingAdapter(FakeWealthfolioClient()).known_gaps()
    assert set(gaps) == {
        CAP_TAXONOMY_MERGE_IMPORT,
        CAP_CATEGORY_PATCH,
        CAP_CATEGORY_READ_ONE,
        CAP_BUDGET_PERIOD_LIST,
    }
    assert gaps is not KNOWN_API_GAPS
