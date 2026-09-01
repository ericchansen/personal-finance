"""Deterministic coverage for the local Ollama categorization agent.

Every fixture here is invented: invented merchants, invented accounts, invented
amounts, an invented model digest. No test in this module opens a socket -- the
Ollama transport is a callable, so the whole client, harness, cache and planner
integration run against a scripted fake.
"""

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from importers.categorize.agent import (
    DEFAULT_MIN_CONFIDENCE,
    AgentCache,
    AgentSuggestion,
    AgentSuggestionSet,
    agent_cache_dir,
    build_messages,
    cache_privacy_problem,
    echo_problem,
    prompt_schema_fingerprint,
    run_agent,
    summarize_agent,
    validate_decision,
    write_agent_review,
    write_agent_suggestions,
)
from importers.categorize.agent_tools import (
    AGENT_ELIGIBLE_REASONS,
    EvidenceAssembler,
    LocalFileMerchantResearch,
    MerchantResearch,
    build_clusters,
    taxonomy_options,
)
from importers.categorize.identity import (
    SourceResolver,
    build_account_bridge,
    build_canonical_index,
)
from importers.categorize.live_history import (
    build_live_history_index,
    build_live_history_scope,
)
from importers.categorize.ollama import (
    DEFAULT_MODEL,
    OllamaClient,
    require_loopback_url,
)
from importers.rebuild.decisions import DecisionError
from importers.rebuild.safety import plan_fingerprint
from importers.simplefin.categorization import (
    AGENT_EVIDENCE_KIND,
    INCOME_TAXONOMY,
    SPENDING_TAXONOMY,
    HistoryIndex,
    build_category_plan,
    evidence_binding,
    in_source_scope,
    rehearse_category_plan,
    validate_agent_seal,
    validate_category_plan,
    write_category_review,
)

from tests.test_source_categorization import (
    ACCOUNT_MAP,
    ACCOUNTS,
    CANONICAL_CHECKING,
    LIVE_CHECKING,
    canonical_row,
    catalogs,
    live_activity,
    staged_rows,
)
from tests.test_simplefin_categorization import StageClient

NOW = datetime(2026, 9, 2, tzinfo=timezone.utc)
HASH_KEY = b"synthetic-private-hmac-key-00003"
START = "2026-08-01"
END = "2026-08-31"
MODEL_DIGEST = "sha256:0000000000000000000000000000000000000000000000000000000000000abc"

MERCHANT = "Zephyr Bakehouse"
OTHER_MERCHANT = "Nimbus Fuel Depot"


# -- fakes ------------------------------------------------------------------


class FakeTransport:
    """A scripted Ollama server. Records every request body it was given."""

    def __init__(self, replies, *, models=None, version="0.12.0-synthetic"):
        self.replies = list(replies)
        self.models = (
            models
            if models is not None
            else [
                {
                    "name": DEFAULT_MODEL,
                    "digest": MODEL_DIGEST,
                    "details": {
                        "parameter_size": "30.5B",
                        "quantization_level": "Q4_K_M",
                    },
                }
            ]
        )
        self.version = version
        self.requests = []
        self.chat_calls = 0

    def __call__(self, method, url, body, timeout):
        payload = json.loads(body.decode()) if body else None
        self.requests.append((method, url, payload))
        if url.endswith("/api/version"):
            return 200, json.dumps({"version": self.version}).encode()
        if url.endswith("/api/tags"):
            return 200, json.dumps({"models": self.models}).encode()
        if url.endswith("/api/show"):
            return 200, json.dumps({"details": {}}).encode()
        if url.endswith("/api/chat"):
            self.chat_calls += 1
            reply = self.replies[min(self.chat_calls - 1, len(self.replies) - 1)]
            content = reply if isinstance(reply, str) else json.dumps(reply)
            return 200, json.dumps({"message": {"content": content}}).encode()
        raise AssertionError(f"unexpected request {method} {url}")

    @property
    def prompts(self):
        return [
            payload
            for method, url, payload in self.requests
            if url.endswith("/api/chat")
        ]


def decision(
    *,
    category_id="groceries",
    taxonomy_id=SPENDING_TAXONOMY,
    confidence=0.95,
    rationale="A neighbourhood food retailer, so everyday food shopping.",
    recommend_rule=True,
    rule_match_type="exact",
    flags=(),
    decision_kind="categorize",
):
    return {
        "decision": decision_kind,
        "taxonomyId": taxonomy_id,
        "categoryId": category_id,
        "confidence": confidence,
        "rationale": rationale,
        "recommendRule": recommend_rule,
        "ruleMatchType": rule_match_type,
        "uncertaintyFlags": list(flags),
    }


def client_for(replies, **kwargs):
    return OllamaClient(
        "http://127.0.0.1:11434",
        transport=FakeTransport(replies, **kwargs),
        model=DEFAULT_MODEL,
    )


# -- plan fixtures ----------------------------------------------------------


def unresolved_plan(
    activities,
    *,
    canonical=None,
    history=None,
    live_history=None,
    agent_suggestions=None,
    assignments=None,
    evidence=(),
):
    """A deterministic plan whose only unresolved items are agent-eligible."""
    rows = canonical_for(activities) if canonical is None else list(canonical)
    index = build_canonical_index(rows)
    bridge = build_account_bridge(canonical_account_map=ACCOUNT_MAP)
    scoped = [row for row in activities if in_source_scope(row, ("*",))]
    resolver = SourceResolver(index, bridge, scoped)
    eligible = [
        row
        for row in scoped
        if row.get("activityType") not in {"TRANSFER_IN", "TRANSFER_OUT"}
    ]
    outflow = sum(Decimal(str(row.get("amount") or 0)) for row in eligible)
    return build_category_plan(
        activities,
        ACCOUNTS,
        {str(row["id"]): [] for row in activities} if assignments is None else assignments,
        catalogs(),
        {"accounts": []},
        history or HistoryIndex(),
        {"categoryAliases": {}, "activityOverrides": {}, "merchantOverrides": {}},
        list(evidence) or [{"path": "synthetic", "sha256": "0" * 64}],
        "synthetic-environment",
        HASH_KEY,
        report_start=START,
        report_end=END,
        spending_account_ids={LIVE_CHECKING},
        current_report={
            "current": {
                "income": "0",
                "outflow": str(outflow),
                "net": str(-outflow),
                "count": len(eligible),
            },
            "spendingBreakdown": [],
            "incomeBreakdown": [],
        },
        current_uncategorized_count=len(eligible),
        generated_at=NOW,
        resolver=resolver,
        source_systems=("*",),
        live_history=live_history,
        agent_suggestions=agent_suggestions,
    )


def canonical_for(activities):
    """Canonical rows that resolve identity but carry no reviewed category.

    This is the realistic shape of an unresolved item: the activity *did* join
    to a canonical transaction, that transaction simply has no reviewed category
    to carry over, so the planner runs out of evidence and abstains.
    """
    rows = []
    for activity in activities:
        key = str(activity.get("idempotencyKey") or "")
        if not key.startswith("monarch:"):
            continue
        rows.append(
            canonical_row(
                key,
                description=str(activity.get("comment") or ""),
                date=str(activity.get("date") or "")[:10],
                amount=f"-{activity.get('amount')}",
                category="",
                category_id="",
            )
        )
    return rows


def recurring_activities(count=3, *, merchant=MERCHANT, start_day=4, prefix="row"):
    return [
        live_activity(
            f"activity-{prefix}-{index}",
            f"monarch:{prefix}-{index}",
            description=merchant,
            date=f"2026-08-{start_day + index:02d}",
            amount=f"{12 + index}.75",
        )
        for index in range(count)
    ]


def reseal(plan):
    """Re-fingerprint a hand-edited plan, as a tamperer would have to."""
    plan.pop("planFingerprint", None)
    plan["planFingerprint"] = plan_fingerprint(plan)
    return plan


def reseal_agent(plan):
    """Re-fingerprint the model seal *and* the plan, the full forgery."""
    seal = plan["ollamaAgent"]
    seal.pop("sealFingerprint", None)
    seal["sealFingerprint"] = plan_fingerprint(seal)
    return reseal(plan)


def agent_set(activities, replies, **kwargs):
    plan = unresolved_plan(activities)
    client = client_for(replies)
    return plan, client, run_agent(
        plan=plan,
        activities=activities,
        accounts=ACCOUNTS,
        catalogs=catalogs(),
        client=client,
        merchant_hash_key=HASH_KEY,
        generated_at=NOW,
        **kwargs,
    )


# -- loopback enforcement ---------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.5:11434",
        "https://ollama.example.com",
        "http://example.com:11434",
        "ftp://127.0.0.1:11434",
        "http://user:pass@127.0.0.1:11434",
        "http://127.0.0.1:11434/?token=abc",
    ],
)
def test_non_loopback_endpoints_are_refused(url):
    with pytest.raises(DecisionError):
        require_loopback_url(url)


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:11434", "http://localhost:11434", "http://[::1]:11434"],
)
def test_loopback_endpoints_are_accepted(url):
    assert require_loopback_url(url).startswith(("http://127.0.0.1", "http://localhost", "http://[::1]"))


def test_client_refuses_a_remote_endpoint_at_construction():
    with pytest.raises(DecisionError):
        OllamaClient("http://198.51.100.9:11434", transport=FakeTransport([]))


def test_health_reports_a_missing_model_without_raising():
    client = client_for([], models=[{"name": "llama3:8b", "digest": "sha256:abc"}])
    health = client.health()

    assert health.reachable is True
    assert health.model_present is False
    assert health.ready is False
    assert "not installed" in health.detail
    assert "ollama" in health.summary()


def test_health_reports_an_unreachable_endpoint_without_raising():
    def refuse(method, url, body, timeout):
        raise DecisionError("connection refused")

    client = OllamaClient("http://127.0.0.1:11434", transport=refuse)
    health = client.health()

    assert health.reachable is False
    assert health.ready is False
    assert health.detail == "connection refused"


def test_model_fingerprint_changes_with_the_model_digest():
    first = client_for([]).model_fingerprint()
    second = client_for(
        [],
        models=[
            {
                "name": DEFAULT_MODEL,
                "digest": "sha256:different",
                "details": {"parameter_size": "30.5B", "quantization_level": "Q4_K_M"},
            }
        ],
    ).model_fingerprint()

    assert first["digest"] == MODEL_DIGEST
    assert first["fingerprint"] != second["fingerprint"]


def test_model_fingerprint_refuses_a_missing_model():
    client = client_for([], models=[])
    with pytest.raises(DecisionError, match="not installed"):
        client.model_fingerprint()


# -- clustering -------------------------------------------------------------


def test_recurring_variants_of_one_merchant_become_one_cluster():
    activities = recurring_activities(3)
    plan = unresolved_plan(activities)

    clusters, skipped = build_clusters(plan, activities, ACCOUNTS, HASH_KEY)

    assert len(clusters) == 1
    assert len(clusters[0].members) == 3
    assert clusters[0].taxonomy_id == SPENDING_TAXONOMY
    assert not skipped


def test_one_merchant_in_two_directions_becomes_two_clusters():
    activities = [
        *recurring_activities(2),
        live_activity(
            "activity-credit",
            "monarch:row-credit",
            description=MERCHANT,
            kind="DEPOSIT",
            date="2026-08-20",
            amount="9.00",
        ),
    ]
    plan = unresolved_plan(activities)

    clusters, _ = build_clusters(plan, activities, ACCOUNTS, HASH_KEY)

    assert {cluster.taxonomy_id for cluster in clusters} == {
        SPENDING_TAXONOMY,
        INCOME_TAXONOMY,
    }


def test_structural_activities_never_reach_a_cluster():
    activities = [
        *recurring_activities(1),
        live_activity(
            "activity-transfer",
            "monarch:row-transfer",
            description="Internal Move",
            subtype="internal_transfer",
        ),
        live_activity(
            "activity-gap",
            f"gap:{LIVE_CHECKING}:2026-08-09",
            description="Balance Gap",
        ),
    ]
    plan = unresolved_plan(activities)

    clusters, skipped = build_clusters(plan, activities, ACCOUNTS, HASH_KEY)

    assert len(clusters) == 1
    assert clusters[0].members[0].activity_id == "activity-row-0"
    assert not any("transfer" in cluster.cluster_id for cluster in clusters)
    assert "structural:structural-subtype" in skipped


def test_only_evidence_exhaustion_reasons_are_eligible():
    assert AGENT_ELIGIBLE_REASONS == {
        "no-history",
        "insufficient-history",
        "unmapped-category",
    }


def test_a_conflicting_history_item_is_never_offered_to_the_model():
    history = HistoryIndex()
    history.add(CANONICAL_CHECKING, MERCHANT, "Groceries", "monarch:a")
    history.add(CANONICAL_CHECKING, MERCHANT, "Gas & Fuel", "monarch:b")
    activities = recurring_activities(1)
    plan = unresolved_plan(activities, history=history)

    clusters, skipped = build_clusters(plan, activities, ACCOUNTS, HASH_KEY)

    assert plan["manualItems"][0]["reason"] == "conflicting-history"
    assert clusters == []
    assert skipped["conflicting-history"] == 1


def test_a_cluster_never_serializes_the_merchant():
    activities = recurring_activities(2)
    plan = unresolved_plan(activities)
    clusters, _ = build_clusters(plan, activities, ACCOUNTS, HASH_KEY)
    cluster = clusters[0]

    document = json.dumps(cluster.as_document())

    assert MERCHANT.casefold() not in document.casefold()
    assert "zephyr" not in repr(cluster).casefold()
    assert cluster.prompt_payee == MERCHANT.casefold()


# -- tool evidence ----------------------------------------------------------


def assembler_for(**kwargs):
    return EvidenceAssembler(catalogs=catalogs(), **kwargs)


def test_taxonomy_options_expose_the_hierarchy():
    nested = {
        SPENDING_TAXONOMY: [
            {"id": "food", "name": "Food"},
            {"id": "groceries", "name": "Groceries", "parentId": "food"},
        ],
        INCOME_TAXONOMY: [],
    }

    options = taxonomy_options(nested, SPENDING_TAXONOMY)

    assert {row["categoryId"]: row["path"] for row in options} == {
        "food": "Food",
        "groceries": "Food > Groceries",
    }


def test_assembled_evidence_names_every_tool_and_hides_the_merchant():
    activities = recurring_activities(3)
    plan = unresolved_plan(activities)
    clusters, _ = build_clusters(plan, activities, ACCOUNTS, HASH_KEY)
    assembler = assembler_for()

    evidence = assembler.assemble(clusters[0])

    assert set(evidence["tools"]) == {
        "live_taxonomy",
        "transaction_shape",
        "recurrence",
        "source_evidence",
        "category_history",
        "structural_guard",
        "merchant_research",
    }
    assert evidence["tools"]["recurrence"]["activityCount"] == 3
    assert evidence["tools"]["transaction_shape"]["direction"] == "debit"
    assert evidence["tools"]["source_evidence"]["sourceSystems"] == ["monarch"]
    assert MERCHANT.casefold() not in json.dumps(evidence).casefold()
    assert evidence["evidenceHash"]


def test_evidence_hash_changes_when_the_taxonomy_changes():
    activities = recurring_activities(1)
    plan = unresolved_plan(activities)
    clusters, _ = build_clusters(plan, activities, ACCOUNTS, HASH_KEY)
    reduced = {
        SPENDING_TAXONOMY: [{"id": "groceries", "name": "Groceries"}],
        INCOME_TAXONOMY: [],
    }

    first = assembler_for().assemble(clusters[0])["evidenceHash"]
    second = EvidenceAssembler(catalogs=reduced).assemble(clusters[0])["evidenceHash"]

    assert first != second


def test_history_tool_reports_below_threshold_live_evidence():
    activities = recurring_activities(1)
    trained = live_activity(
        "activity-trained",
        "monarch:row-trained",
        description=MERCHANT,
        date="2026-07-01",
    )
    scope = build_live_history_scope(
        end_date=END, lookback_months=24, account_ids={LIVE_CHECKING}, min_evidence=2
    )
    live = build_live_history_index(
        [trained],
        {"activity-trained": [{"taxonomyId": SPENDING_TAXONOMY, "categoryId": "groceries"}]},
        ACCOUNTS,
        HASH_KEY,
        scope=scope,
        spending_account_ids={LIVE_CHECKING},
    )
    plan = unresolved_plan(activities, live_history=live)
    clusters, _ = build_clusters(plan, activities, ACCOUNTS, HASH_KEY)

    evidence = assembler_for(live_history=live).assemble(clusters[0])
    live_tool = evidence["tools"]["category_history"]["live"]

    assert live_tool["reasons"] == ["insufficient-live-history"]
    assert live_tool["belowThresholdCandidates"] == [
        {"categoryId": "groceries", "scope": "account", "observationCount": 1}
    ]


def test_merchant_research_is_disabled_by_default():
    activities = recurring_activities(1)
    plan = unresolved_plan(activities)
    clusters, _ = build_clusters(plan, activities, ACCOUNTS, HASH_KEY)

    tool = assembler_for().assemble(clusters[0])["tools"]["merchant_research"]

    assert tool == {
        "name": "disabled",
        "enabled": False,
        "performsNetworkRequests": False,
        "facts": None,
    }


def test_local_file_merchant_research_never_claims_network_access(tmp_path):
    path = tmp_path / "merchant-research.json"
    path.write_text(
        json.dumps({MERCHANT: {"industry": "bakery", "note": "food retail"}}),
        encoding="utf-8",
    )
    research = LocalFileMerchantResearch(path)

    assert research.performs_network_requests is False
    assert research.describe()["enabled"] is True
    assert research.lookup(MERCHANT) == {"industry": "bakery", "note": "food retail"}
    assert research.lookup("Unknown Merchant") is None


def test_default_research_backend_returns_nothing():
    assert MerchantResearch().lookup(MERCHANT) is None
    assert MerchantResearch().performs_network_requests is False


# -- prompt and schema validation ------------------------------------------


def test_only_the_prompt_carries_the_raw_merchant():
    activities = recurring_activities(2)
    plan = unresolved_plan(activities)
    clusters, _ = build_clusters(plan, activities, ACCOUNTS, HASH_KEY)
    assembler = assembler_for()
    evidence = assembler.assemble(clusters[0])

    messages = build_messages(clusters[0], evidence, assembler)

    assert messages[0]["role"] == "system"
    assert MERCHANT.casefold() in messages[1]["content"].casefold()
    assert "allowedCategories" in messages[1]["content"]


def sample_cluster(activities=None):
    activities = activities or recurring_activities(2)
    plan = unresolved_plan(activities)
    clusters, _ = build_clusters(plan, activities, ACCOUNTS, HASH_KEY)
    return clusters[0]


@pytest.mark.parametrize(
    "payload, expected",
    [
        ("not json at all", "malformed-json"),
        ("[]", "not-an-object"),
        (json.dumps({"decision": "categorize"}), "missing-fields"),
        (
            json.dumps({**decision(), "extra": 1}),
            "unexpected-fields",
        ),
        (json.dumps(decision(decision_kind="invent")), "unknown-decision"),
        (json.dumps(decision(confidence="NaN")), "confidence-not-finite"),
        (json.dumps(decision(confidence="Infinity")), "confidence-not-finite"),
        (json.dumps(decision(confidence=1.5)), "confidence-out-of-range"),
        (json.dumps(decision(confidence=-0.1)), "confidence-out-of-range"),
        (json.dumps(decision(category_id="not-a-real-category")), "invented-category-id"),
        (json.dumps(decision(taxonomy_id=INCOME_TAXONOMY)), "direction-mismatch"),
        (json.dumps(decision(flags=("made-up-flag",))), "unknown-uncertainty-flag"),
        (json.dumps(decision(rule_match_type="fuzzy")), "unknown-rule-match-type"),
        (json.dumps({**decision(), "recommendRule": "yes"}), "recommend-rule-invalid"),
        (json.dumps({**decision(), "uncertaintyFlags": "none"}), "uncertainty-flags-invalid"),
        (json.dumps(decision(rationale="")), "rationale-empty"),
        (json.dumps(decision(rationale="x" * 400)), "rationale-too-long"),
        (
            json.dumps(decision(rationale="See https://example.com for details.")),
            "rationale-contains-url",
        ),
        (
            json.dumps(decision(rationale="Charged 1275 every month.")),
            "rationale-contains-long-number",
        ),
        (
            json.dumps(decision(rationale=f"{MERCHANT} sells bread.")),
            "rationale-echoes-merchant",
        ),
        (
            json.dumps(decision(rationale="Zephyr is a bakery brand.")),
            "rationale-echoes-merchant",
        ),
        (
            json.dumps(decision(rationale="Seen on live-checking regularly.")),
            "rationale-echoes-transaction-data",
        ),
    ],
)
def test_invalid_model_output_is_rejected_with_a_named_reason(payload, expected):
    cluster = sample_cluster()

    result, problem = validate_decision(payload, cluster, assembler_for())

    assert result is None
    assert problem == expected


def test_a_valid_decision_is_normalized_against_the_live_catalog():
    cluster = sample_cluster()

    result, problem = validate_decision(
        json.dumps(decision(confidence=0.9349)), cluster, assembler_for()
    )

    assert problem == ""
    assert result["categoryId"] == "groceries"
    assert result["categoryName"] == "Groceries"
    assert result["confidence"] == "0.93"
    assert result["taxonomyId"] == SPENDING_TAXONOMY


def test_an_abstention_carries_no_category_or_confidence():
    cluster = sample_cluster()

    result, problem = validate_decision(
        json.dumps(
            decision(
                decision_kind="abstain",
                category_id="groceries",
                confidence=0.99,
                rationale="The evidence does not identify what was purchased.",
                flags=("unfamiliar-merchant",),
            )
        ),
        cluster,
        assembler_for(),
    )

    assert problem == ""
    assert result["categoryId"] == ""
    assert result["confidence"] == "0.00"
    assert result["recommendRule"] is False
    assert result["uncertaintyFlags"] == ["unfamiliar-merchant"]


def test_echo_problem_catches_amounts_in_any_written_form():
    cluster = sample_cluster()
    amount = f"{cluster.members[0].amount:f}"

    assert echo_problem(f"Costs {amount} each time.", cluster)
    assert echo_problem("Recurring food retail purchase.", cluster) == ""


def test_a_rationale_may_reuse_a_word_the_taxonomy_itself_publishes():
    """A trade word already written next to the rationale is not a leak.

    "Gas & Fuel" is printed in the plan as ``categoryName``. A rationale saying
    "A fuel retailer." reveals nothing further, even when the payee happens to
    contain the same word -- whereas the distinctive part of the payee stays
    forbidden either way.
    """
    activities = recurring_activities(2, merchant="Nimbus Fuel Depot", prefix="fuel")
    plan = unresolved_plan(activities)
    clusters, _ = build_clusters(plan, activities, ACCOUNTS, HASH_KEY)
    cluster = clusters[0]
    vocabulary = assembler_for().category_vocabulary(SPENDING_TAXONOMY)

    assert echo_problem("A fuel retailer.", cluster, vocabulary) == ""
    assert echo_problem("A fuel retailer.", cluster) == "rationale-echoes-merchant"
    assert (
        echo_problem("Nimbus is a fuel brand.", cluster, vocabulary)
        == "rationale-echoes-merchant"
    )
    assert (
        echo_problem("A depot selling fuel.", cluster, vocabulary)
        == "rationale-echoes-merchant"
    )


def test_the_taxonomy_vocabulary_covers_names_and_paths():
    vocabulary = assembler_for().category_vocabulary(SPENDING_TAXONOMY)

    assert "fuel" in vocabulary
    assert "groceries" in vocabulary
    assert "mortgage" in vocabulary
    assert "zephyr" not in vocabulary


# -- retry ------------------------------------------------------------------


def test_a_rejected_answer_is_retried_once_and_then_accepted():
    activities = recurring_activities(2)
    transport_replies = [
        json.dumps(decision(category_id="invented")),
        json.dumps(decision()),
    ]
    _plan, client, suggestions = agent_set(activities, transport_replies)

    assert client._transport.chat_calls == 2
    assert len(suggestions.suggestions) == 1
    assert suggestions.suggestions[0].attempts == 2
    correction = client._transport.prompts[-1]["messages"][-1]["content"]
    assert "invented-category-id" in correction
    assert "copy one verbatim" in correction


def test_a_leaky_rationale_is_corrected_with_an_actionable_hint():
    activities = recurring_activities(2)
    replies = [
        json.dumps(decision(rationale=f"{MERCHANT} is a bakery.")),
        json.dumps(decision(rationale="A neighbourhood food shop.")),
    ]
    _plan, client, suggestions = agent_set(activities, replies)

    correction = client._transport.prompts[-1]["messages"][-1]["content"]
    assert "rationale-echoes-merchant" in correction
    assert "name only the kind of business" in correction
    assert suggestions.suggestions[0].rationale == "A neighbourhood food shop."


def test_retries_are_bounded_and_end_in_an_abstention():
    activities = recurring_activities(2)
    _plan, client, suggestions = agent_set(
        activities, [json.dumps(decision(category_id="invented"))], max_attempts=3
    )

    assert client._transport.chat_calls == 3
    assert suggestions.suggestions == []
    assert suggestions.metrics()["outcomeCounts"] == {
        "rejected:invented-category-id": 1
    }
    assert suggestions.applicable_count == 0


def test_max_attempts_must_be_at_least_one():
    activities = recurring_activities(1)
    with pytest.raises(DecisionError):
        agent_set(activities, [json.dumps(decision())], max_attempts=0)


def test_confidence_threshold_must_be_a_probability():
    activities = recurring_activities(1)
    with pytest.raises(DecisionError):
        agent_set(
            activities, [json.dumps(decision())], min_confidence=Decimal("1.5")
        )


# -- cache ------------------------------------------------------------------


def private_dir(tmp_path):
    data_dir = tmp_path / "private-data"
    data_dir.mkdir()
    return data_dir


def test_a_cached_decision_is_reused_without_asking_the_model(tmp_path):
    data_dir = private_dir(tmp_path)
    activities = recurring_activities(2)
    cache = AgentCache(agent_cache_dir(data_dir), data_dir)

    _plan, first_client, first = agent_set(
        activities, [json.dumps(decision())], cache=cache
    )
    _plan2, second_client, second = agent_set(
        activities, [json.dumps(decision())], cache=cache
    )

    assert first_client._transport.chat_calls == 1
    assert second_client._transport.chat_calls == 0
    assert second.suggestions[0].cached is True
    assert second.suggestions[0].category_id == first.suggestions[0].category_id
    assert cache.hits == 1


def test_a_cache_file_contains_no_merchant_text(tmp_path):
    data_dir = private_dir(tmp_path)
    activities = recurring_activities(2)
    cache = AgentCache(agent_cache_dir(data_dir), data_dir)

    agent_set(activities, [json.dumps(decision())], cache=cache)

    files = list(agent_cache_dir(data_dir).glob("*.json"))
    assert len(files) == 1
    blob = files[0].read_text(encoding="utf-8").casefold()
    assert MERCHANT.casefold() not in blob
    assert "zephyr" not in blob
    assert "bakehouse" not in blob
    record = json.loads(files[0].read_text(encoding="utf-8"))
    assert record["cacheKey"] == files[0].stem
    assert record["decision"]["categoryId"] == "groceries"


def test_cache_refuses_to_store_a_record_carrying_merchant_text(tmp_path):
    data_dir = private_dir(tmp_path)
    cache = AgentCache(agent_cache_dir(data_dir), data_dir)
    cluster = sample_cluster()
    leaky = decision(rationale="ok")
    leaky["categoryName"] = MERCHANT

    with pytest.raises(DecisionError, match="refusing to cache private data"):
        cache.put(
            "key",
            cluster,
            leaky,
            model_fingerprint="fp",
            model=DEFAULT_MODEL,
            model_digest=MODEL_DIGEST,
            prompt_fingerprint="pf",
            evidence_hash="eh",
            stored_at=NOW,
        )
    assert not list(agent_cache_dir(data_dir).glob("*.json"))


def test_a_cache_entry_that_leaked_merchant_text_is_ignored_on_read(tmp_path):
    data_dir = private_dir(tmp_path)
    cache = AgentCache(agent_cache_dir(data_dir), data_dir)
    cluster = sample_cluster()
    key = AgentCache.key(
        model_fingerprint="fp", prompt_fingerprint="pf", evidence_hash="eh"
    )
    (agent_cache_dir(data_dir) / f"{key}.json").write_text(
        json.dumps({
            "schemaVersion": 1,
            "cacheKey": key,
            "decision": {"categoryId": "groceries", "note": MERCHANT},
        }),
        encoding="utf-8",
    )

    assert cache.get(key, cluster) is None
    assert cache.rejected == 1


def test_cache_key_changes_with_model_prompt_and_evidence():
    base = dict(model_fingerprint="a", prompt_fingerprint="b", evidence_hash="c")

    assert AgentCache.key(**base) != AgentCache.key(**{**base, "model_fingerprint": "z"})
    assert AgentCache.key(**base) != AgentCache.key(**{**base, "prompt_fingerprint": "z"})
    assert AgentCache.key(**base) != AgentCache.key(**{**base, "evidence_hash": "z"})


def test_a_different_model_does_not_reuse_a_cached_decision(tmp_path):
    data_dir = private_dir(tmp_path)
    activities = recurring_activities(2)
    cache = AgentCache(agent_cache_dir(data_dir), data_dir)
    plan = unresolved_plan(activities)

    for models in (None, [{"name": DEFAULT_MODEL, "digest": "sha256:moved"}]):
        client = client_for([json.dumps(decision())], models=models)
        run_agent(
            plan=plan,
            activities=activities,
            accounts=ACCOUNTS,
            catalogs=catalogs(),
            client=client,
            merchant_hash_key=HASH_KEY,
            cache=cache,
            generated_at=NOW,
        )

    assert len(list(agent_cache_dir(data_dir).glob("*.json"))) == 2
    assert cache.hits == 0


def test_the_cache_must_live_under_the_private_data_directory(tmp_path):
    data_dir = private_dir(tmp_path)
    with pytest.raises(DecisionError):
        AgentCache(tmp_path / "elsewhere", data_dir)


def test_cache_privacy_problem_flags_merchant_tokens():
    cluster = sample_cluster()

    assert cache_privacy_problem(
        {"decision": {"rationale": "A bakehouse."}}, cluster
    )
    assert cache_privacy_problem(
        {"decision": {"rationale": "A grocery store."}}, cluster
    ) == ""


def test_cache_privacy_allows_shared_taxonomy_vocabulary():
    cluster = sample_cluster(
        recurring_activities(2, merchant=OTHER_MERCHANT, prefix="fuel")
    )
    record = {
        "decision": {
            "categoryName": "Gas & Fuel",
            "rationale": "A fuel retailer.",
        }
    }

    assert cache_privacy_problem(
        record, cluster, frozenset({"fuel", "gas", "retailer"})
    ) == ""


# -- orchestration ----------------------------------------------------------


def test_one_decision_covers_every_recurring_activity():
    activities = recurring_activities(4)
    _plan, client, suggestions = agent_set(activities, [json.dumps(decision())])

    assert client._transport.chat_calls == 1
    assert suggestions.applicable_count == 4
    assert suggestions.suggestions[0].activity_count == 4


def test_below_threshold_suggestions_stay_manual():
    activities = recurring_activities(2)
    _plan, _client, suggestions = agent_set(
        activities, [json.dumps(decision(confidence=0.55))]
    )

    assert suggestions.applicable_count == 0
    assert len(suggestions.below_threshold) == 1
    assert suggestions.suggestion_for("activity-row-0") is None
    assert suggestions.metrics()["outcomeCounts"] == {"below-threshold": 1}


def test_an_abstention_produces_no_applicable_suggestion():
    activities = recurring_activities(2)
    _plan, _client, suggestions = agent_set(
        activities,
        [
            json.dumps(
                decision(
                    decision_kind="abstain",
                    rationale="Not enough evidence to name a category.",
                )
            )
        ],
    )

    assert suggestions.applicable_count == 0
    assert suggestions.metrics()["abstainedCount"] == 1


def test_a_cluster_budget_leaves_the_rest_unattempted():
    activities = [
        *recurring_activities(3),
        *recurring_activities(
            1, merchant=OTHER_MERCHANT, start_day=20, prefix="other"
        ),
    ]
    _plan, client, suggestions = agent_set(
        activities, [json.dumps(decision())], max_clusters=1
    )

    assert client._transport.chat_calls == 1
    assert suggestions.metrics()["outcomeCounts"] == {
        "applicable": 1,
        "not-attempted-cluster-budget": 1,
    }


def test_the_largest_cluster_is_asked_first():
    activities = [
        *recurring_activities(
            1, merchant=OTHER_MERCHANT, start_day=20, prefix="other"
        ),
        *recurring_activities(3),
    ]
    plan = unresolved_plan(activities)
    client = client_for([json.dumps(decision())])

    suggestions = run_agent(
        plan=plan,
        activities=activities,
        accounts=ACCOUNTS,
        catalogs=catalogs(),
        client=client,
        merchant_hash_key=HASH_KEY,
        max_clusters=1,
        generated_at=NOW,
    )

    assert suggestions.suggestions[0].activity_count == 3


def test_the_agent_is_never_asked_about_a_resolved_activity():
    history = HistoryIndex()
    for index in range(2):
        history.add(CANONICAL_CHECKING, MERCHANT, "Groceries", f"monarch:seed-{index}")
    activities = recurring_activities(2)
    plan = unresolved_plan(activities, history=history)
    client = client_for([json.dumps(decision())])

    suggestions = run_agent(
        plan=plan,
        activities=activities,
        accounts=ACCOUNTS,
        catalogs=catalogs(),
        client=client,
        merchant_hash_key=HASH_KEY,
        generated_at=NOW,
    )

    assert plan["metrics"]["autoCount"] == 2
    assert client._transport.chat_calls == 0
    assert suggestions.metrics()["clusterCount"] == 0


# -- planner integration ----------------------------------------------------


def applied_plan(activities, replies, *, evidence=(), **kwargs):
    plan = unresolved_plan(activities, evidence=evidence)
    client = client_for(replies)
    suggestions = run_agent(
        plan=plan,
        activities=activities,
        accounts=ACCOUNTS,
        catalogs=catalogs(),
        client=client,
        merchant_hash_key=HASH_KEY,
        generated_at=NOW,
        **kwargs,
    )
    return (
        unresolved_plan(
            activities, agent_suggestions=suggestions, evidence=evidence
        ),
        suggestions,
    )


def real_evidence(tmp_path):
    """A real, hashable evidence file so a plan survives full validation."""
    data_dir = private_dir(tmp_path)
    path = data_dir / "synthetic-evidence.json"
    path.write_text(json.dumps({"synthetic": True}), encoding="utf-8")
    return evidence_binding([path])


def test_applied_suggestions_become_sealed_candidates():
    activities = recurring_activities(3)
    plan, suggestions = applied_plan(activities, [json.dumps(decision())])

    assert plan["metrics"]["autoCount"] == 3
    assert plan["metrics"]["agentCount"] == 3
    assert plan["metrics"]["agentClusterCount"] == 1
    candidate = plan["autoCandidates"][0]
    assert candidate["evidenceKind"] == AGENT_EVIDENCE_KIND
    assert candidate["categoryId"] == "groceries"
    assert candidate["agentModelDigest"] == MODEL_DIGEST
    assert candidate["agentEvidenceHash"] == suggestions.suggestions[0].evidence_hash
    assert plan["ollamaAgent"]["minConfidence"] == "0.90"


def test_a_sealed_agent_plan_validates(tmp_path):
    activities = recurring_activities(2)
    applied, _ = applied_plan(
        activities, [json.dumps(decision())], evidence=real_evidence(tmp_path)
    )

    validate_category_plan(applied)

    assert applied["metrics"]["agentCount"] == 2


def test_deterministic_evidence_always_outranks_the_model():
    history = HistoryIndex()
    for index in range(2):
        history.add(CANONICAL_CHECKING, MERCHANT, "Gas & Fuel", f"monarch:seed-{index}")
    activities = recurring_activities(2)
    plan = unresolved_plan(activities, history=history)
    client = client_for([json.dumps(decision())])
    suggestions = run_agent(
        plan=plan,
        activities=activities,
        accounts=ACCOUNTS,
        catalogs=catalogs(),
        client=client,
        merchant_hash_key=HASH_KEY,
        generated_at=NOW,
    )

    applied = unresolved_plan(
        activities, history=history, agent_suggestions=suggestions
    )

    assert applied["metrics"]["agentCount"] == 0
    assert applied["autoCandidates"][0]["categoryId"] == "fuel"
    assert applied["autoCandidates"][0]["evidenceKind"] == "account-history"


def test_live_history_outranks_the_model():
    activities = recurring_activities(2)
    trained = [
        live_activity(
            f"activity-trained-{index}",
            f"monarch:trained-{index}",
            description=MERCHANT,
            date=f"2026-07-0{index + 1}",
        )
        for index in range(2)
    ]
    scope = build_live_history_scope(
        end_date=END, lookback_months=24, account_ids={LIVE_CHECKING}, min_evidence=2
    )
    live = build_live_history_index(
        trained,
        {
            row["id"]: [{"taxonomyId": SPENDING_TAXONOMY, "categoryId": "fuel"}]
            for row in trained
        },
        ACCOUNTS,
        HASH_KEY,
        scope=scope,
        spending_account_ids={LIVE_CHECKING},
    )
    plan = unresolved_plan(activities, live_history=live)
    client = client_for([json.dumps(decision())])
    suggestions = run_agent(
        plan=plan,
        activities=activities,
        accounts=ACCOUNTS,
        catalogs=catalogs(),
        client=client,
        merchant_hash_key=HASH_KEY,
        live_history=live,
        generated_at=NOW,
    )

    applied = unresolved_plan(
        activities, live_history=live, agent_suggestions=suggestions
    )

    assert client._transport.chat_calls == 0
    assert applied["metrics"]["agentCount"] == 0
    assert applied["metrics"]["liveHistoryCount"] == 2


def test_a_direction_mismatched_suggestion_is_refused_by_the_planner():
    activities = recurring_activities(2)
    mismatched = AgentSuggestion(
        cluster_id="income_sources:deadbeef",
        merchant_hash="deadbeef",
        taxonomy_id=INCOME_TAXONOMY,
        category_id="salary",
        category_name="Salary",
        confidence="0.99",
        activity_count=2,
        activity_ids=[row["id"] for row in activities],
        evidence_hash="synthetic-evidence-hash",
        rationale="Looks like regular pay.",
        recommend_rule=False,
        rule_match_type="none",
        uncertainty_flags=(),
        model=DEFAULT_MODEL,
        model_digest=MODEL_DIGEST,
        cached=False,
        attempts=1,
    )
    suggestions = AgentSuggestionSet(
        model={"model": DEFAULT_MODEL, "digest": MODEL_DIGEST},
        endpoint={
            "baseUrl": "http://127.0.0.1:11434",
            "loopback": True,
            "webResearchEnabled": False,
        },
        prompt_fingerprint=prompt_schema_fingerprint(),
        min_confidence=DEFAULT_MIN_CONFIDENCE,
        tool_manifest=[],
        clusters=[],
        suggestions=[mismatched],
        skipped={},
        outcomes={},
        cache_stats={},
        generated_at=NOW,
    )

    plan = unresolved_plan(activities, agent_suggestions=suggestions)

    assert plan["metrics"]["agentCount"] == 0
    assert plan["metrics"]["agentAbstentionCounts"] == {"agent-direction-mismatch": 2}
    assert plan["ollamaAgent"]["suggestions"] == []
    assert {row["reason"] for row in plan["manualItems"]} == {"no-history"}


def test_a_suggestion_naming_a_category_the_catalog_lost_is_refused():
    activities = recurring_activities(2)
    stale = AgentSuggestion(
        cluster_id="spending_categories:deadbeef",
        merchant_hash="deadbeef",
        taxonomy_id=SPENDING_TAXONOMY,
        category_id="category-that-was-deleted",
        category_name="Gone",
        confidence="0.99",
        activity_count=2,
        activity_ids=[row["id"] for row in activities],
        evidence_hash="synthetic-evidence-hash",
        rationale="Stale suggestion.",
        recommend_rule=False,
        rule_match_type="none",
        uncertainty_flags=(),
        model=DEFAULT_MODEL,
        model_digest=MODEL_DIGEST,
        cached=False,
        attempts=1,
    )
    suggestions = AgentSuggestionSet(
        model={"model": DEFAULT_MODEL, "digest": MODEL_DIGEST},
        endpoint={
            "baseUrl": "http://127.0.0.1:11434",
            "loopback": True,
            "webResearchEnabled": False,
        },
        prompt_fingerprint=prompt_schema_fingerprint(),
        min_confidence=DEFAULT_MIN_CONFIDENCE,
        tool_manifest=[],
        clusters=[],
        suggestions=[stale],
        skipped={},
        outcomes={},
        cache_stats={},
        generated_at=NOW,
    )

    plan = unresolved_plan(activities, agent_suggestions=suggestions)

    assert plan["metrics"]["agentCount"] == 0
    assert plan["metrics"]["agentAbstentionCounts"] == {"agent-unknown-category": 2}


def test_an_unsealed_agent_candidate_is_refused():
    activities = recurring_activities(2)
    plan, _ = applied_plan(activities, [json.dumps(decision())])
    plan["ollamaAgent"]["suggestions"] = []

    with pytest.raises(DecisionError, match="unsealed model evidence"):
        validate_agent_seal(reseal_agent(plan))


def test_a_missing_seal_with_agent_candidates_is_refused():
    activities = recurring_activities(2)
    plan, _ = applied_plan(activities, [json.dumps(decision())])
    plan.pop("ollamaAgent")

    with pytest.raises(DecisionError, match="without sealing them"):
        validate_agent_seal(reseal(plan))


def test_a_non_loopback_seal_is_refused():
    activities = recurring_activities(2)
    plan, _ = applied_plan(activities, [json.dumps(decision())])
    plan["ollamaAgent"]["endpoint"]["loopback"] = False

    with pytest.raises(DecisionError, match="non-loopback endpoint"):
        validate_agent_seal(reseal(plan))


def test_a_web_research_seal_is_refused():
    activities = recurring_activities(2)
    plan, _ = applied_plan(activities, [json.dumps(decision())])
    plan["ollamaAgent"]["endpoint"]["webResearchEnabled"] = True

    with pytest.raises(DecisionError, match="permits web research"):
        validate_agent_seal(reseal(plan))


def test_a_seal_without_a_model_digest_is_refused():
    activities = recurring_activities(2)
    plan, _ = applied_plan(activities, [json.dumps(decision())])
    plan["ollamaAgent"]["model"]["digest"] = ""

    with pytest.raises(DecisionError, match="does not identify its model"):
        validate_agent_seal(reseal(plan))


def test_a_candidate_below_the_sealed_threshold_is_refused():
    activities = recurring_activities(2)
    plan, _ = applied_plan(activities, [json.dumps(decision(confidence=0.91))])
    plan["ollamaAgent"]["minConfidence"] = "0.99"
    plan["ollamaAgent"]["sealFingerprint"] = plan_fingerprint({
        key: value
        for key, value in plan["ollamaAgent"].items()
        if key != "sealFingerprint"
    })

    with pytest.raises(DecisionError, match="below its threshold"):
        validate_agent_seal(reseal(plan))


def test_a_tampered_seal_fingerprint_is_refused():
    activities = recurring_activities(2)
    plan, _ = applied_plan(activities, [json.dumps(decision())])
    plan["ollamaAgent"]["suggestions"][0]["categoryId"] = "fuel"

    with pytest.raises(DecisionError, match="seal fingerprint is invalid"):
        validate_agent_seal(reseal(plan))


def test_a_candidate_naming_a_different_model_digest_is_refused():
    activities = recurring_activities(2)
    plan, _ = applied_plan(activities, [json.dumps(decision())])
    plan["autoCandidates"][0]["agentModelDigest"] = "sha256:someone-elses-weights"

    with pytest.raises(DecisionError, match="different model digest"):
        validate_agent_seal(reseal(plan))


def test_a_plan_without_a_model_pass_still_validates():
    activities = recurring_activities(2)
    plan = unresolved_plan(activities)

    assert "ollamaAgent" not in plan
    assert plan["metrics"]["agentCount"] == 0


# -- artifacts --------------------------------------------------------------


def test_the_suggestion_artifact_and_review_contain_no_merchant_text(tmp_path):
    data_dir = private_dir(tmp_path)
    activities = recurring_activities(3)
    _plan, _client, suggestions = agent_set(activities, [json.dumps(decision())])
    document = suggestions.as_document()

    artifact = write_agent_suggestions(data_dir, document)
    review = write_agent_review(data_dir, document)

    for path in (artifact, review):
        blob = path.read_text(encoding="utf-8").casefold()
        assert MERCHANT.casefold() not in blob
        assert "zephyr" not in blob
        assert "bakehouse" not in blob
    assert document["categoriesAssigned"] == 0
    assert document["endpoint"]["webResearchEnabled"] is False
    assert "loopback" in review.read_text(encoding="utf-8")
    assert "agent model=" in summarize_agent(document)


def test_the_category_review_reports_the_model_pass(tmp_path):
    data_dir = private_dir(tmp_path)
    activities = recurring_activities(2)
    plan, _ = applied_plan(activities, [json.dumps(decision())])

    path = write_category_review(data_dir, plan)
    body = path.read_text(encoding="utf-8")

    assert "## Local model (Ollama) decisions" in body
    assert MODEL_DIGEST[:16] in body
    assert MERCHANT.casefold() not in body.casefold()


def test_the_prompt_schema_fingerprint_is_stable():
    assert prompt_schema_fingerprint() == prompt_schema_fingerprint()
    assert len(prompt_schema_fingerprint()) == 64


def test_an_agent_sealed_plan_rehearses_like_any_other(tmp_path):
    """A model decision gets no special treatment downstream.

    The whole point of sealing into the existing plan is that staging rehearsal
    and production promotion do not need to know a model was involved: the
    candidate carries the same portable identity and is applied, verified and
    rolled back by exactly the same machinery.
    """
    activities = recurring_activities(2)
    plan, _ = applied_plan(
        activities, [json.dumps(decision())], evidence=real_evidence(tmp_path)
    )

    client = StageClient(staged_rows(activities))
    first = rehearse_category_plan(
        client, plan, {CANONICAL_CHECKING: "stage-account"}, HASH_KEY, generated_at=NOW
    )
    second = rehearse_category_plan(
        client, plan, {CANONICAL_CHECKING: "stage-account"}, HASH_KEY, generated_at=NOW
    )

    assert first["status"] == "applied"
    assert first["appliedCount"] == 2
    assert second["status"] == "already-applied"
    assert client.puts == 2
    assert MERCHANT.casefold() not in json.dumps(first).casefold()


def test_an_agent_sealed_rehearsal_rolls_back_a_partial_failure(tmp_path):
    activities = recurring_activities(2)
    plan, _ = applied_plan(
        activities, [json.dumps(decision())], evidence=real_evidence(tmp_path)
    )
    client = StageClient(staged_rows(activities), fail_on_put=2)

    with pytest.raises(RuntimeError, match="synthetic failure"):
        rehearse_category_plan(
            client,
            plan,
            {CANONICAL_CHECKING: "stage-account"},
            HASH_KEY,
            generated_at=NOW,
        )

    assert all(rows == [] for rows in client.assignments.values())


# -- CLI wiring -------------------------------------------------------------


def cli_plan_args(data_dir, **overrides):
    from tests.test_categorize_cli import plan_args

    args = plan_args(
        data_dir,
        ollama=True,
        ollama_url="http://127.0.0.1:11434",
        ollama_model=DEFAULT_MODEL,
        ollama_timeout=30.0,
        ollama_num_ctx=4096,
        agent_min_confidence=DEFAULT_MIN_CONFIDENCE,
        agent_max_attempts=3,
        agent_max_clusters=None,
        no_agent_cache=False,
        merchant_research="none",
        merchant_research_path=None,
        apply_agent_suggestions=False,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def cli_fixture(tmp_path, replies, monkeypatch):
    from tests.test_categorize_cli import FakeSpendingClient, private_data_dir
    import importers.categorize.cli as cli

    rows = [canonical_row("monarch:row-1", description=MERCHANT, category="", category_id="")]
    data_dir = private_data_dir(tmp_path, rows)
    client = FakeSpendingClient([
        live_activity("activity-1", "monarch:row-1", description=MERCHANT)
    ])
    transport = FakeTransport(replies)
    monkeypatch.setattr(
        cli,
        "_ollama_client",
        lambda args: OllamaClient(
            args.ollama_url, model=args.ollama_model, transport=transport
        ),
    )
    return data_dir, client, transport


def test_agent_plan_writes_suggestions_without_assigning(tmp_path, monkeypatch, capsys):
    import importers.categorize.cli as cli

    data_dir, client, transport = cli_fixture(
        tmp_path, [json.dumps(decision())], monkeypatch
    )

    assert cli.cmd_plan(cli_plan_args(data_dir), client) == 0

    output = capsys.readouterr().out
    assert "agentSuggestions=" in output
    assert "agentApplied=no (suggestions are plan-only)" in output
    assert transport.chat_calls == 1
    suggestion_path = next((data_dir / "ollama-agent").glob("agent-suggestions-*.json"))
    document = json.loads(suggestion_path.read_text(encoding="utf-8"))
    assert document["categoriesAssigned"] == 0
    assert document["suggestions"][0]["categoryId"] == "groceries"
    plan = json.loads(
        next(
            (data_dir / "normalized" / "simplefin").glob("category-plan-*.json")
        ).read_text(encoding="utf-8")
    )
    assert "ollamaAgent" not in plan
    assert plan["metrics"]["autoCount"] == 0
    assert MERCHANT.casefold() not in json.dumps(document).casefold()


def test_agent_plan_seals_suggestions_when_explicitly_applied(
    tmp_path, monkeypatch, capsys
):
    import importers.categorize.cli as cli

    data_dir, client, _transport = cli_fixture(
        tmp_path, [json.dumps(decision())], monkeypatch
    )

    assert (
        cli.cmd_plan(cli_plan_args(data_dir, apply_agent_suggestions=True), client) == 0
    )

    output = capsys.readouterr().out
    assert "agentApplied=1" in output
    plan = json.loads(
        next(
            (data_dir / "normalized" / "simplefin").glob("category-plan-*.json")
        ).read_text(encoding="utf-8")
    )
    assert plan["metrics"]["agentCount"] == 1
    assert plan["ollamaAgent"]["endpoint"]["loopback"] is True
    assert MERCHANT.casefold() not in json.dumps(plan).casefold()


def test_agent_plan_refuses_an_unavailable_model(tmp_path, monkeypatch):
    import importers.categorize.cli as cli
    from tests.test_categorize_cli import FakeSpendingClient, private_data_dir

    rows = [canonical_row("monarch:row-1", description=MERCHANT, category="", category_id="")]
    data_dir = private_data_dir(tmp_path, rows)
    client = FakeSpendingClient([
        live_activity("activity-1", "monarch:row-1", description=MERCHANT)
    ])
    monkeypatch.setattr(
        cli,
        "_ollama_client",
        lambda args: OllamaClient(
            args.ollama_url,
            model=args.ollama_model,
            transport=FakeTransport([], models=[]),
        ),
    )

    with pytest.raises(DecisionError, match="local model is not ready"):
        cli.cmd_plan(cli_plan_args(data_dir), client)


def test_agent_health_reports_readiness(monkeypatch, capsys):
    import importers.categorize.cli as cli
    from types import SimpleNamespace

    args = SimpleNamespace(
        ollama_url="http://127.0.0.1:11434",
        ollama_model=DEFAULT_MODEL,
        ollama_timeout=30.0,
        ollama_num_ctx=4096,
    )
    monkeypatch.setattr(
        cli,
        "_ollama_client",
        lambda args: OllamaClient(
            args.ollama_url, model=args.ollama_model, transport=FakeTransport([])
        ),
    )

    assert cli.cmd_agent_health(args) == 0
    assert "ready=yes" in capsys.readouterr().out


def test_agent_health_fails_when_the_model_is_missing(monkeypatch, capsys):
    import importers.categorize.cli as cli
    from types import SimpleNamespace

    args = SimpleNamespace(
        ollama_url="http://127.0.0.1:11434",
        ollama_model=DEFAULT_MODEL,
        ollama_timeout=30.0,
        ollama_num_ctx=4096,
    )
    monkeypatch.setattr(
        cli,
        "_ollama_client",
        lambda args: OllamaClient(
            args.ollama_url,
            model=args.ollama_model,
            transport=FakeTransport([], models=[{"name": "llama3:8b"}]),
        ),
    )

    assert cli.cmd_agent_health(args) == 1
    output = capsys.readouterr().out
    assert "ready=no" in output
    assert "ollama pull" in output


def test_the_cli_refuses_a_remote_model_endpoint():
    from importers.categorize.cli import main

    with pytest.raises(SystemExit):
        main(["agent-health", "--ollama-url", "http://198.51.100.9:11434"])


def test_an_empty_pass_still_seals_its_model_and_threshold():
    empty = AgentSuggestionSet(
        model={"model": DEFAULT_MODEL, "digest": MODEL_DIGEST},
        endpoint={"baseUrl": "http://127.0.0.1:11434", "loopback": True,
                  "webResearchEnabled": False},
        prompt_fingerprint=prompt_schema_fingerprint(),
        min_confidence=DEFAULT_MIN_CONFIDENCE,
        tool_manifest=[],
        clusters=[],
        suggestions=[],
        skipped={},
        outcomes={},
        cache_stats={},
        generated_at=NOW,
    )

    seal = empty.seal()

    assert seal["minConfidence"] == "0.90"
    assert seal["suggestions"] == []
    assert seal["sealFingerprint"]
