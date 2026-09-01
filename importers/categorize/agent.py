"""A purpose-built local categorization agent over a loopback Ollama server.

This is a harness, not a prompt. For every merchant the deterministic planner
could not resolve it:

1. **clusters** the unresolved activities by keyed merchant digest *and*
   direction, so one decision covers every recurring variant of a payee;
2. **assembles local tool evidence** (see :mod:`importers.categorize.agent_tools`)
   -- allowed taxonomy and hierarchy, direction, account kind, recurrence and
   amount band, source-system provenance, exact live and canonical history, and
   the structural guards;
3. **asks one schema-constrained question** over loopback, with the raw payee
   present only in that request body;
4. **validates the answer strictly** against the live taxonomy, the direction,
   the confidence bounds and a raw-data echo check, retrying a bounded number of
   times with the specific violation named;
5. **caches** the validated decision under the private data directory, keyed by
   model identity, prompt/schema identity and evidence hash, containing no
   merchant text at all;
6. **seals** what it produced -- model digest, prompt schema fingerprint,
   confidence threshold and per-cluster evidence hash -- so a suggestion that
   later reaches a sealed plan is auditable months afterwards.

The model never overrides a deterministic decision. It is consulted only where
private rules, reviewed canonical carryover, canonical merchant consensus and
Wealthfolio's own live history have all already declined, and its output is
re-checked against the same structural and direction guards afterwards.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Sequence

from importers.categorize.agent_tools import (
    AGENT_ELIGIBLE_REASONS,
    EvidenceAssembler,
    MerchantCluster,
    MerchantResearch,
    build_clusters,
)
from importers.categorize.ollama import OllamaClient
from importers.rebuild.decisions import DecisionError
from importers.rebuild.safety import plan_fingerprint, validate_private_output
from importers.simplefin.categorization import (
    AGENT_EVIDENCE_KIND,
    REPO_ROOT,
    validate_data_dir,
)

#: Bumped whenever the prompt text or the response schema changes meaning. It is
#: part of every cache key, so a prompt edit invalidates prior decisions instead
#: of silently reusing answers to a different question.
PROMPT_TEMPLATE_VERSION = 1

#: Deliberately high. A suggestion below this stays visible as manual review
#: rather than being written into a sealed plan.
DEFAULT_MIN_CONFIDENCE = Decimal("0.90")

#: One retry after the first failure. Bounded on purpose: a model that cannot
#: satisfy a schema twice is not going to satisfy it on the ninth attempt, and
#: an unbounded self-correction loop is an unbounded local compute bill.
DEFAULT_MAX_ATTEMPTS = 3

#: Upper bound on the rationale, enforced locally as well as in the schema.
MAX_RATIONALE_CHARS = 240

#: The only uncertainty vocabulary a decision may use. A model-invented flag is
#: rejected rather than stored, so downstream review can rely on the values.
UNCERTAINTY_FLAGS = (
    "ambiguous-merchant",
    "direction-unclear",
    "insufficient-evidence",
    "multiple-plausible-categories",
    "possible-subscription",
    "possible-transfer",
    "unfamiliar-merchant",
)

RULE_MATCH_TYPES = ("exact", "regex", "none")

SYSTEM_PROMPT = (
    "You classify a merchant into exactly one category from a fixed list.\n"
    "Rules you must follow:\n"
    "1. Answer with JSON matching the supplied schema and nothing else.\n"
    "2. categoryId must be copied verbatim from allowedCategories. Never "
    "invent, translate or abbreviate an id.\n"
    "3. taxonomyId must equal the taxonomyId given in transaction_shape. Do "
    "not classify a debit as income or a credit as spending.\n"
    "4. Bank descriptions are mostly unfamiliar local business names. Do NOT "
    "abstain merely because you do not recognise the brand. First ask whether "
    "you know the merchant; if you do not, read the descriptive words in the "
    "name and classify from those. \"Bakehouse\", \"cafe\", \"grocer\" and "
    "\"market\" name food businesses; \"fuel\", \"gas\" and \"petro\" name "
    "fuel retailers; \"pharmacy\", \"dental\" and \"clinic\" name health "
    "providers, and so on for every trade.\n"
    "5. Abstain only when the description carries no interpretable signal at "
    "all -- an opaque reference code, a bare number, or a name that could "
    "plausibly belong to several unrelated categories. Abstaining then is "
    "correct and expected; guessing is not.\n"
    "6. confidence is your probability that the category is right, between 0 "
    "and 1. Use 0.90 or above only when you recognise the merchant, or the "
    "name plainly states its trade. Use 0.60 to 0.89 when inferring from a "
    "partial signal. Use below 0.60 when it is close to a guess.\n"
    "7. Prefer the most specific category that clearly fits. Fall back to a "
    "general one only when no specific category applies.\n"
    "8. rationale must be at most one short sentence naming the KIND OF "
    "BUSINESS, written as if you had never seen the description. Do not quote, "
    "spell out or partially repeat the merchant name, and do not include any "
    "amount, date, account identifier, URL, or number longer than three "
    "digits. Write \"A fuel retailer.\", not \"The name contains 'fuel'.\"; "
    "write \"A neighbourhood food shop.\", not \"Bakehouse means bakery.\"\n"
    "9. recommendRule is true only when this merchant always means this "
    "category, so a transparent exact-match rule would be safe.\n"
    "10. Structural money movements (transfers between the owner's own "
    "accounts, credit-card payments, loan payments, savings and investment "
    "transfers) are excluded before you see them. If the evidence still looks "
    "like one, abstain and flag possible-transfer."
)

DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["categorize", "abstain"]},
        "taxonomyId": {"type": "string"},
        "categoryId": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "rationale": {"type": "string", "maxLength": MAX_RATIONALE_CHARS},
        "recommendRule": {"type": "boolean"},
        "ruleMatchType": {"type": "string", "enum": list(RULE_MATCH_TYPES)},
        "uncertaintyFlags": {
            "type": "array",
            "items": {"type": "string", "enum": list(UNCERTAINTY_FLAGS)},
        },
    },
    "required": [
        "decision",
        "taxonomyId",
        "categoryId",
        "confidence",
        "rationale",
        "recommendRule",
        "ruleMatchType",
        "uncertaintyFlags",
    ],
    "additionalProperties": False,
}

_DIGIT_RUN = re.compile(r"\d{4,}")
_TOKEN = re.compile(r"[a-z0-9]+")
_URL = re.compile(r"(https?://|www\.)", re.IGNORECASE)


def prompt_schema_fingerprint() -> str:
    """Identity of the exact question shape a cached decision answered."""
    return plan_fingerprint({
        "promptTemplateVersion": PROMPT_TEMPLATE_VERSION,
        "system": SYSTEM_PROMPT,
        "schema": DECISION_SCHEMA,
    })


def _confidence(value: Any) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise DecisionError("confidence is not a number") from None
    if not number.is_finite():
        raise DecisionError("confidence is not finite")
    return number


def format_confidence(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01')):f}"


# -- output validation ------------------------------------------------------


def echo_problem(
    text: str,
    cluster: MerchantCluster,
    allowed_vocabulary: frozenset[str] = frozenset(),
) -> str:
    """Name the first way a rationale leaks raw transaction data, if any.

    The rationale is the only free text a model contributes, and it is written
    into a private plan and a private review. Neither should ever carry the
    merchant back out in prose, so a rationale that repeats the *distinctive*
    part of the payee, an amount, a date, an identifier or a URL is rejected and
    re-asked.

    ``allowed_vocabulary`` is the word set the live taxonomy itself publishes --
    "fuel", "groceries", "mortgage". A payee token that is also a category word
    is exempt, because the plan already states the category next to the
    rationale: repeating it reveals nothing that was not already written down.
    Every other token, which is precisely the part that identifies *this*
    merchant rather than its trade, stays forbidden.
    """
    lowered = str(text or "").casefold()
    if not lowered.strip():
        return "rationale-empty"
    if len(text) > MAX_RATIONALE_CHARS:
        return "rationale-too-long"
    if _URL.search(lowered):
        return "rationale-contains-url"
    if _DIGIT_RUN.search(lowered):
        return "rationale-contains-long-number"
    payee = cluster.prompt_payee.casefold()
    if payee and payee not in allowed_vocabulary and payee in lowered:
        return "rationale-echoes-merchant"
    tokens = {
        token
        for token in _TOKEN.findall(payee)
        if len(token) >= 4 and token not in allowed_vocabulary
    }
    if any(token in lowered for token in tokens):
        return "rationale-echoes-merchant"
    identifiers = {cluster.merchant_hash[:16].casefold()}
    for member in cluster.members:
        identifiers.add(member.activity_id.casefold())
        identifiers.add(member.account_id.casefold())
        identifiers.add(member.canonical_account_id.casefold())
        if member.date:
            identifiers.add(member.date.casefold())
        amount = f"{member.amount:f}"
        identifiers.add(amount)
        identifiers.add(amount.replace(".", ""))
    if any(value and value in lowered for value in identifiers):
        return "rationale-echoes-transaction-data"
    return ""


#: Actionable corrections for the failures a model can realistically fix. A
#: bare reason code tells it *that* it failed; these tell it what to do instead.
RETRY_HINTS = {
    "rationale-echoes-merchant": (
        "the rationale repeated the merchant name; name only the kind of "
        "business, as in \"A fuel retailer.\""
    ),
    "rationale-echoes-transaction-data": (
        "the rationale repeated an amount, date or identifier; describe only "
        "the kind of business"
    ),
    "rationale-contains-long-number": (
        "the rationale contained a long number; remove every figure"
    ),
    "invented-category-id": (
        "the categoryId is not in allowedCategories; copy one verbatim"
    ),
    "direction-mismatch": (
        "the taxonomyId must be copied from transaction_shape unchanged"
    ),
}


def validate_decision(
    raw: str,
    cluster: MerchantCluster,
    assembler: EvidenceAssembler,
) -> tuple[dict[str, Any] | None, str]:
    """Turn raw model output into a trusted decision, or name why it is not.

    Schema-constrained decoding limits shape, never truthfulness, so every
    guard that matters is re-applied here: the category must exist in the live
    taxonomy, the taxonomy must match the direction the local evidence derived,
    the confidence must be a finite probability, and the rationale must not echo
    private data back out.
    """
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None, "malformed-json"
    if not isinstance(payload, dict):
        return None, "not-an-object"
    unknown = set(payload) - set(DECISION_SCHEMA["properties"])
    if unknown:
        return None, "unexpected-fields"
    missing = set(DECISION_SCHEMA["required"]) - set(payload)
    if missing:
        return None, "missing-fields"

    decision = str(payload.get("decision") or "")
    if decision not in {"categorize", "abstain"}:
        return None, "unknown-decision"

    try:
        confidence = _confidence(payload.get("confidence"))
    except DecisionError:
        return None, "confidence-not-finite"
    if not (Decimal(0) <= confidence <= Decimal(1)):
        return None, "confidence-out-of-range"

    flags = payload.get("uncertaintyFlags")
    if not isinstance(flags, list) or any(not isinstance(flag, str) for flag in flags):
        return None, "uncertainty-flags-invalid"
    if any(flag not in UNCERTAINTY_FLAGS for flag in flags):
        return None, "unknown-uncertainty-flag"

    if not isinstance(payload.get("recommendRule"), bool):
        return None, "recommend-rule-invalid"
    match_type = str(payload.get("ruleMatchType") or "")
    if match_type not in RULE_MATCH_TYPES:
        return None, "unknown-rule-match-type"

    rationale = str(payload.get("rationale") or "")
    problem = echo_problem(
        rationale, cluster, assembler.category_vocabulary(cluster.taxonomy_id)
    )
    if problem:
        return None, problem

    normalized = {
        "decision": decision,
        "taxonomyId": cluster.taxonomy_id,
        "categoryId": "",
        "categoryName": "",
        "confidence": format_confidence(confidence),
        "rationale": rationale.strip(),
        "recommendRule": bool(payload["recommendRule"]),
        "ruleMatchType": match_type,
        "uncertaintyFlags": sorted(set(flags)),
    }
    if decision == "abstain":
        # An abstention carries no category and no usable confidence: the model
        # is declining, not expressing certainty about declining.
        normalized["confidence"] = "0.00"
        normalized["recommendRule"] = False
        normalized["ruleMatchType"] = "none"
        return normalized, ""

    if str(payload.get("taxonomyId") or "") != cluster.taxonomy_id:
        return None, "direction-mismatch"
    category_id = str(payload.get("categoryId") or "")
    if not category_id:
        return None, "missing-category"
    if category_id not in assembler.allowed_category_ids(cluster.taxonomy_id):
        return None, "invented-category-id"
    option = next(
        row
        for row in assembler.options_for(cluster.taxonomy_id)
        if row["categoryId"] == category_id
    )
    normalized["categoryId"] = category_id
    normalized["categoryName"] = option["name"]
    return normalized, ""


# -- suggestions ------------------------------------------------------------


class AgentSuggestion:
    """One validated model decision, shaped like every other evidence source.

    ``evidence_kind``, ``confidence`` and ``evidence_count`` exist so the
    planner's rescue path can treat a model decision and a live-history
    consensus identically; ``as_candidate_fields`` is what keeps their sealed
    provenance completely separate.
    """

    __slots__ = (
        "cluster_id",
        "merchant_hash",
        "taxonomy_id",
        "category_id",
        "category_name",
        "_confidence",
        "activity_count",
        "activity_ids",
        "evidence_hash",
        "rationale",
        "recommend_rule",
        "rule_match_type",
        "uncertainty_flags",
        "model",
        "model_digest",
        "cached",
        "attempts",
    )

    def __init__(
        self,
        *,
        cluster_id: str,
        merchant_hash: str,
        taxonomy_id: str,
        category_id: str,
        category_name: str,
        confidence: str,
        activity_count: int,
        activity_ids: Sequence[str],
        evidence_hash: str,
        rationale: str,
        recommend_rule: bool,
        rule_match_type: str,
        uncertainty_flags: Sequence[str],
        model: str,
        model_digest: str,
        cached: bool,
        attempts: int,
    ) -> None:
        self.cluster_id = cluster_id
        self.merchant_hash = merchant_hash
        self.taxonomy_id = taxonomy_id
        self.category_id = category_id
        self.category_name = category_name
        self._confidence = confidence
        self.activity_count = activity_count
        self.activity_ids = tuple(activity_ids)
        self.evidence_hash = evidence_hash
        self.rationale = rationale
        self.recommend_rule = recommend_rule
        self.rule_match_type = rule_match_type
        self.uncertainty_flags = tuple(uncertainty_flags)
        self.model = model
        self.model_digest = model_digest
        self.cached = cached
        self.attempts = attempts

    @property
    def evidence_kind(self) -> str:
        return AGENT_EVIDENCE_KIND

    @property
    def confidence(self) -> str:
        return self._confidence

    @property
    def confidence_value(self) -> Decimal:
        return Decimal(self._confidence)

    @property
    def evidence_count(self) -> int:
        return self.activity_count

    def as_candidate_fields(self) -> dict[str, Any]:
        return {
            "agentEvidenceHash": self.evidence_hash,
            "agentClusterId": self.cluster_id,
            "agentModel": self.model,
            "agentModelDigest": self.model_digest,
            "agentRationale": self.rationale,
            "agentRecommendRule": self.recommend_rule,
            "agentRuleMatchType": self.rule_match_type,
            "agentUncertaintyFlags": list(self.uncertainty_flags),
        }

    def as_sealed_document(self) -> dict[str, Any]:
        return {
            "evidenceHash": self.evidence_hash,
            "clusterId": self.cluster_id,
            "merchantHash": self.merchant_hash,
            "taxonomyId": self.taxonomy_id,
            "categoryId": self.category_id,
            "confidence": self.confidence,
            "activityCount": self.activity_count,
            "sampleActivityIds": sorted(self.activity_ids),
            "rationale": self.rationale,
            "recommendRule": self.recommend_rule,
            "ruleMatchType": self.rule_match_type,
            "uncertaintyFlags": list(self.uncertainty_flags),
        }


class AgentCache:
    """Decisions keyed by model identity, question identity and evidence.

    A cache entry names *what was asked of which weights about which evidence*.
    Change the model, the prompt, the schema or any tool output and the key
    changes, so a stale answer can never be reused for a different question.

    Entries contain hashes, ids and the validated decision. They never contain a
    merchant description, and that is asserted on the way in and on the way out
    rather than assumed.
    """

    schema_version = 1

    def __init__(self, root: Path, data_dir: Path) -> None:
        self.root = validate_private_output(Path(root), Path(data_dir), REPO_ROOT)
        self.root.mkdir(parents=True, exist_ok=True)
        self.hits = 0
        self.misses = 0
        self.rejected = 0

    @staticmethod
    def key(
        *,
        model_fingerprint: str,
        prompt_fingerprint: str,
        evidence_hash: str,
    ) -> str:
        return plan_fingerprint({
            "modelFingerprint": model_fingerprint,
            "promptSchemaFingerprint": prompt_fingerprint,
            "promptTemplateVersion": PROMPT_TEMPLATE_VERSION,
            "evidenceHash": evidence_hash,
        })

    def _path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def get(
        self,
        key: str,
        cluster: MerchantCluster,
        allowed_vocabulary: frozenset[str] = frozenset(),
    ) -> dict[str, Any] | None:
        path = self._path(key)
        if not path.is_file():
            self.misses += 1
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.rejected += 1
            return None
        if (
            not isinstance(record, dict)
            or record.get("schemaVersion") != self.schema_version
            or record.get("cacheKey") != key
            or not isinstance(record.get("decision"), dict)
        ):
            self.rejected += 1
            return None
        if cache_privacy_problem(record, cluster, allowed_vocabulary):
            # A cache file that contains merchant text is not repairable; it is
            # ignored rather than trusted, and the decision is re-derived.
            self.rejected += 1
            return None
        self.hits += 1
        return record["decision"]

    def put(
        self,
        key: str,
        cluster: MerchantCluster,
        decision: dict[str, Any],
        *,
        model_fingerprint: str,
        model: str,
        model_digest: str,
        prompt_fingerprint: str,
        evidence_hash: str,
        stored_at: datetime,
        allowed_vocabulary: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        record = {
            "schemaVersion": self.schema_version,
            "cacheKey": key,
            "modelFingerprint": model_fingerprint,
            "model": model,
            "modelDigest": model_digest,
            "promptSchemaFingerprint": prompt_fingerprint,
            "promptTemplateVersion": PROMPT_TEMPLATE_VERSION,
            "evidenceHash": evidence_hash,
            "merchantHash": cluster.merchant_hash,
            "taxonomyId": cluster.taxonomy_id,
            "decision": decision,
            "storedAt": stored_at.isoformat(),
        }
        problem = cache_privacy_problem(record, cluster, allowed_vocabulary)
        if problem:
            raise DecisionError(f"refusing to cache private data: {problem}")
        expected = self.key(
            model_fingerprint=model_fingerprint,
            prompt_fingerprint=prompt_fingerprint,
            evidence_hash=evidence_hash,
        )
        if expected != key:
            raise DecisionError("agent cache key does not describe its own record")
        self._path(key).write_text(json.dumps(record, indent=2), encoding="utf-8")
        return record


def cache_privacy_problem(
    record: dict[str, Any],
    cluster: MerchantCluster,
    allowed_vocabulary: frozenset[str] = frozenset(),
) -> str:
    """Name the first way a cache record would leak merchant text, if any."""
    try:
        blob = json.dumps(record, sort_keys=True, default=str).casefold()
    except (TypeError, ValueError):
        return "cache-record-not-serializable"
    payee = cluster.prompt_payee.casefold()
    if payee and payee in blob:
        return "cache-record-contains-merchant"
    decision = record.get("decision") if isinstance(record.get("decision"), dict) else {}
    authored = " ".join(
        str(decision.get(field) or "").casefold()
        for field in ("categoryName", "rationale")
    )
    tokens = {
        token
        for token in _TOKEN.findall(payee)
        if len(token) >= 4
        and token.isalpha()
        and token not in allowed_vocabulary
    }
    if any(token in authored for token in tokens):
        return "cache-record-contains-merchant-token"
    return ""


class AgentSuggestionSet:
    """Everything one agent pass produced, and what a plan may use from it.

    The set is the object the planner consults. It answers one question --
    "is there a decision confident enough to apply to this activity?" -- and it
    seals the provenance of every decision that was actually used.
    """

    def __init__(
        self,
        *,
        model: dict[str, str],
        endpoint: dict[str, Any],
        prompt_fingerprint: str,
        min_confidence: Decimal,
        tool_manifest: list[dict[str, str]],
        clusters: list[dict[str, Any]],
        suggestions: Sequence[AgentSuggestion],
        skipped: Counter[str],
        outcomes: Counter[str],
        cache_stats: dict[str, int],
        eligible_reasons: Iterable[str] = AGENT_ELIGIBLE_REASONS,
        generated_at: datetime | None = None,
    ) -> None:
        self.model = model
        self.endpoint = endpoint
        self.prompt_fingerprint = prompt_fingerprint
        self.min_confidence = min_confidence
        self.tool_manifest = tool_manifest
        self.clusters = clusters
        self.suggestions = list(suggestions)
        self.skipped = skipped
        self.outcomes = outcomes
        self.cache_stats = cache_stats
        self.eligible_reasons = sorted(eligible_reasons)
        self.generated_at = generated_at or datetime.now(timezone.utc)
        self._by_activity: dict[str, AgentSuggestion] = {}
        for suggestion in self.suggestions:
            if suggestion.confidence_value < self.min_confidence:
                continue
            if not suggestion.category_id:
                continue
            for activity_id in suggestion.activity_ids:
                self._by_activity[activity_id] = suggestion

    # -- planner interface -------------------------------------------------

    def suggestion_for(self, activity_id: str) -> AgentSuggestion | None:
        return self._by_activity.get(str(activity_id or ""))

    @property
    def applicable_count(self) -> int:
        return len(self._by_activity)

    @property
    def below_threshold(self) -> list[AgentSuggestion]:
        return [
            suggestion
            for suggestion in self.suggestions
            if suggestion.category_id
            and suggestion.confidence_value < self.min_confidence
        ]

    def metrics(self) -> dict[str, Any]:
        return {
            "clusterCount": len(self.clusters),
            "activityCount": sum(
                int(row.get("activityCount") or 0) for row in self.clusters
            ),
            "suggestionCount": len(self.suggestions),
            "applicableActivityCount": self.applicable_count,
            "belowThresholdCount": len(self.below_threshold),
            "abstainedCount": sum(
                1 for row in self.suggestions if not row.category_id
            ),
            "outcomeCounts": dict(sorted(self.outcomes.items())),
            "skippedManualReasonCounts": dict(sorted(self.skipped.items())),
            "cache": dict(sorted(self.cache_stats.items())),
        }

    # -- documents ---------------------------------------------------------

    def seal(self, used: Iterable[AgentSuggestion] = ()) -> dict[str, Any]:
        """Seal only the decisions a plan actually relied on."""
        sealed: dict[str, dict[str, Any]] = {}
        for suggestion in used:
            sealed.setdefault(
                suggestion.evidence_hash, suggestion.as_sealed_document()
            )
        document = {
            "schemaVersion": 1,
            "endpoint": dict(self.endpoint),
            "model": dict(self.model),
            "promptSchemaFingerprint": self.prompt_fingerprint,
            "promptTemplateVersion": PROMPT_TEMPLATE_VERSION,
            "minConfidence": format_confidence(self.min_confidence),
            "eligibleManualReasons": list(self.eligible_reasons),
            "toolManifest": list(self.tool_manifest),
            "metrics": {
                **self.metrics(),
                "sealedSuggestionCount": len(sealed),
            },
            "suggestions": sorted(
                sealed.values(), key=lambda row: row["evidenceHash"]
            ),
        }
        document["sealFingerprint"] = plan_fingerprint(document)
        return document

    def as_document(self) -> dict[str, Any]:
        """The standalone, merchant-redacted suggestion artifact."""
        document = {
            "schemaVersion": 1,
            "mode": "ollama-agent-suggestions",
            "generatedAt": self.generated_at.isoformat(),
            "productionMutated": False,
            "categoriesAssigned": 0,
            "endpoint": dict(self.endpoint),
            "model": dict(self.model),
            "promptSchemaFingerprint": self.prompt_fingerprint,
            "promptTemplateVersion": PROMPT_TEMPLATE_VERSION,
            "minConfidence": format_confidence(self.min_confidence),
            "eligibleManualReasons": list(self.eligible_reasons),
            "toolManifest": list(self.tool_manifest),
            "clusters": list(self.clusters),
            "suggestions": [
                {
                    **suggestion.as_sealed_document(),
                    "applicable": (
                        bool(suggestion.category_id)
                        and suggestion.confidence_value >= self.min_confidence
                    ),
                    "cached": suggestion.cached,
                    "attempts": suggestion.attempts,
                }
                for suggestion in sorted(
                    self.suggestions, key=lambda row: row.cluster_id
                )
            ],
            "metrics": self.metrics(),
        }
        document["suggestionFingerprint"] = plan_fingerprint(document)
        return document


# -- orchestration ----------------------------------------------------------


def build_messages(
    cluster: MerchantCluster,
    evidence: dict[str, Any],
    assembler: EvidenceAssembler,
) -> list[dict[str, str]]:
    """The single question asked of the local model.

    This is the only structure in the harness that contains the raw payee, and
    it exists only as a request body sent to a validated loopback address.
    """
    question = {
        "merchant": cluster.prompt_payee,
        "transaction_shape": evidence["tools"]["transaction_shape"],
        "recurrence": evidence["tools"]["recurrence"],
        "source_evidence": evidence["tools"]["source_evidence"],
        "category_history": evidence["tools"]["category_history"],
        "structural_guard": evidence["tools"]["structural_guard"],
        "merchant_research": evidence["tools"]["merchant_research"],
        "allowedCategories": assembler.options_for(cluster.taxonomy_id),
    }
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": json.dumps(question, indent=2, sort_keys=True),
        },
    ]


def _decide(
    client: OllamaClient,
    cluster: MerchantCluster,
    evidence: dict[str, Any],
    assembler: EvidenceAssembler,
    *,
    max_attempts: int,
) -> tuple[dict[str, Any] | None, str, int]:
    """Ask, validate, and correct a bounded number of times."""
    messages = build_messages(cluster, evidence, assembler)
    problem = "no-attempt"
    for attempt in range(1, max_attempts + 1):
        raw = client.chat_json(messages, DECISION_SCHEMA)
        decision, problem = validate_decision(raw, cluster, assembler)
        if decision is not None:
            return decision, "", attempt
        if attempt == max_attempts:
            break
        messages = [
            *messages,
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": (
                    f"That answer was rejected: {problem}"
                    + (f" -- {RETRY_HINTS[problem]}" if problem in RETRY_HINTS else "")
                    + ". Re-read the rules, copy a categoryId verbatim from "
                    "allowedCategories, keep taxonomyId exactly as given, and "
                    "answer again with JSON only. Abstain if you are unsure."
                ),
            },
        ]
    return None, problem, max_attempts


def run_agent(
    *,
    plan: dict[str, Any],
    activities: Sequence[dict[str, Any]],
    accounts: Sequence[dict[str, Any]],
    catalogs: dict[str, list[dict[str, Any]]],
    client: OllamaClient,
    merchant_hash_key: bytes,
    live_history: Any | None = None,
    canonical_history: Any | None = None,
    research: MerchantResearch | None = None,
    cache: AgentCache | None = None,
    min_confidence: Decimal = DEFAULT_MIN_CONFIDENCE,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    max_clusters: int | None = None,
    generated_at: datetime | None = None,
) -> AgentSuggestionSet:
    """Run one full agent pass over a deterministic plan's unresolved items."""
    if not (Decimal(0) < min_confidence <= Decimal(1)):
        raise DecisionError("agent confidence threshold must be within (0, 1]")
    if max_attempts < 1:
        raise DecisionError("agent attempts must be at least one")
    generated_at = generated_at or datetime.now(timezone.utc)
    research = research or MerchantResearch()

    fingerprint = client.model_fingerprint()
    prompt_fingerprint = prompt_schema_fingerprint()
    clusters, skipped = build_clusters(
        plan, activities, accounts, merchant_hash_key
    )
    assembler = EvidenceAssembler(
        catalogs=catalogs,
        live_history=live_history,
        canonical_history=canonical_history,
        research=research,
    )

    # Largest clusters first: when a run is bounded, the decisions that cover
    # the most activities are the ones worth spending the budget on.
    ordered = sorted(
        clusters,
        key=lambda cluster: (-len(cluster.members), cluster.cluster_id),
    )
    outcomes: Counter[str] = Counter()
    cluster_documents: list[dict[str, Any]] = []
    suggestions: list[AgentSuggestion] = []

    for position, cluster in enumerate(ordered):
        evidence = assembler.assemble(cluster)
        document = {
            **evidence["cluster"],
            "evidenceHash": evidence["evidenceHash"],
            "tools": evidence["tools"],
        }
        if max_clusters is not None and position >= max_clusters:
            outcomes["not-attempted-cluster-budget"] += 1
            cluster_documents.append({**document, "status": "not-attempted"})
            continue
        key = AgentCache.key(
            model_fingerprint=fingerprint["fingerprint"],
            prompt_fingerprint=prompt_fingerprint,
            evidence_hash=evidence["evidenceHash"],
        )
        allowed_vocabulary = assembler.category_vocabulary(cluster.taxonomy_id)
        cached = (
            cache.get(key, cluster, allowed_vocabulary)
            if cache is not None
            else None
        )
        attempts = 0
        if cached is not None:
            decision, problem = cached, ""
        else:
            decision, problem, attempts = _decide(
                client, cluster, evidence, assembler, max_attempts=max_attempts
            )
            if decision is not None and cache is not None:
                try:
                    cache.put(
                        key,
                        cluster,
                        decision,
                        model_fingerprint=fingerprint["fingerprint"],
                        model=fingerprint["model"],
                        model_digest=fingerprint["digest"],
                        prompt_fingerprint=prompt_fingerprint,
                        evidence_hash=evidence["evidenceHash"],
                        stored_at=generated_at,
                        allowed_vocabulary=allowed_vocabulary,
                    )
                except DecisionError as exc:
                    if "refusing to cache private data:" not in str(exc):
                        raise
                    # A cache is an optimization, never a prerequisite for a
                    # validated in-memory decision. Keep the suggestion while
                    # refusing to persist a record the privacy guard dislikes.
                    outcomes["cache-skipped-private-data"] += 1
        if decision is None:
            outcomes[f"rejected:{problem}"] += 1
            cluster_documents.append({
                **document,
                "status": "rejected",
                "rejectionReason": problem,
                "attempts": attempts,
            })
            continue
        suggestion = AgentSuggestion(
            cluster_id=cluster.cluster_id,
            merchant_hash=cluster.merchant_hash,
            taxonomy_id=cluster.taxonomy_id,
            category_id=decision["categoryId"],
            category_name=decision["categoryName"],
            confidence=decision["confidence"],
            activity_count=len(cluster.members),
            activity_ids=cluster.activity_ids,
            evidence_hash=evidence["evidenceHash"],
            rationale=decision["rationale"],
            recommend_rule=decision["recommendRule"],
            rule_match_type=decision["ruleMatchType"],
            uncertainty_flags=decision["uncertaintyFlags"],
            model=fingerprint["model"],
            model_digest=fingerprint["digest"],
            cached=cached is not None,
            attempts=attempts,
        )
        suggestions.append(suggestion)
        if not suggestion.category_id:
            status = "abstained"
        elif suggestion.confidence_value >= min_confidence:
            status = "applicable"
        else:
            status = "below-threshold"
        outcomes[status] += 1
        cluster_documents.append({
            **document,
            "status": status,
            "attempts": attempts,
            "cached": cached is not None,
        })

    cluster_documents.sort(key=lambda row: row["clusterId"])
    return AgentSuggestionSet(
        model=fingerprint,
        endpoint={
            "baseUrl": client.base_url,
            "loopback": True,
            "webResearchEnabled": False,
            "researchBackend": research.describe(),
        },
        prompt_fingerprint=prompt_fingerprint,
        min_confidence=min_confidence,
        tool_manifest=assembler.tool_manifest(),
        clusters=cluster_documents,
        suggestions=suggestions,
        skipped=skipped,
        outcomes=outcomes,
        cache_stats={
            "hits": cache.hits if cache else 0,
            "misses": cache.misses if cache else 0,
            "rejected": cache.rejected if cache else 0,
            "enabled": 1 if cache else 0,
        },
        generated_at=generated_at,
    )


# -- artifacts --------------------------------------------------------------


def agent_cache_dir(data_dir: Path) -> Path:
    return validate_data_dir(data_dir) / "ollama-agent" / "cache"


def write_agent_suggestions(data_dir: Path, document: dict[str, Any]) -> Path:
    data_dir = validate_data_dir(data_dir)
    folder = data_dir / "ollama-agent"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromisoformat(document["generatedAt"]).strftime(
        "%Y-%m-%d-%H%M%S-%f"
    )
    path = folder / f"agent-suggestions-{stamp}.json"
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


def write_agent_review(data_dir: Path, document: dict[str, Any]) -> Path:
    """Write the private, merchant-redacted Markdown review of a model pass."""
    data_dir = validate_data_dir(data_dir)
    folder = data_dir / "ollama-agent"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromisoformat(document["generatedAt"]).strftime(
        "%Y-%m-%d-%H%M%S-%f"
    )
    path = folder / f"agent-review-{stamp}.md"
    metrics = document["metrics"]
    model = document["model"]
    endpoint = document["endpoint"]
    lines = [
        "# Local model category suggestions",
        "",
        f"- Suggestion fingerprint: `{document['suggestionFingerprint']}`",
        f"- Model: `{model.get('model', '')}` "
        f"(digest `{str(model.get('digest', ''))[:16]}`, "
        f"{model.get('parameterSize') or 'unknown size'} "
        f"{model.get('quantization') or ''})".rstrip(),
        f"- Model fingerprint: `{model.get('fingerprint', '')}`",
        f"- Endpoint: `{endpoint.get('baseUrl', '')}` "
        f"(loopback: {'yes' if endpoint.get('loopback') else 'no'}, "
        f"web research: {'yes' if endpoint.get('webResearchEnabled') else 'no'})",
        f"- Prompt/schema fingerprint: `{document['promptSchemaFingerprint']}` "
        f"(template v{document['promptTemplateVersion']})",
        f"- Confidence threshold: {document['minConfidence']}",
        f"- Categories assigned by this artifact: "
        f"{document['categoriesAssigned']}",
        "",
        "## Coverage",
        "",
        f"- Merchant clusters: {metrics['clusterCount']} "
        f"covering {metrics['activityCount']} unresolved activities",
        f"- Suggestions at or above threshold: "
        f"{metrics['applicableActivityCount']} activities",
        f"- Suggestions below threshold (manual review): "
        f"{metrics['belowThresholdCount']}",
        f"- Model abstentions: {metrics['abstainedCount']}",
        f"- Cache: {metrics['cache'].get('hits', 0)} hit(s), "
        f"{metrics['cache'].get('misses', 0)} miss(es), "
        f"{metrics['cache'].get('rejected', 0)} rejected",
        "",
        "## Suggestions",
        "",
        "| Cluster | Merchant evidence | Category | Confidence | Applies to | "
        "Rule | Flags | Rationale |",
        "|---|---|---|---:|---:|---|---|---|",
    ]
    lines.extend(
        f"| `{row['clusterId']}` | `{row['merchantHash'][:16]}` | "
        f"{row['categoryId'] or 'abstained'} | {row['confidence']} | "
        f"{row['activityCount']} | "
        f"{'yes' if row['recommendRule'] else 'no'} ({row['ruleMatchType']}) | "
        f"{', '.join(row['uncertaintyFlags']) or 'none'} | {row['rationale']} |"
        for row in document["suggestions"]
    )
    lines.extend([
        "",
        "## Clusters the model was not asked about",
        "",
        "| Manual reason | Activities |",
        "|---|---:|",
    ])
    lines.extend(
        f"| {reason} | {count} |"
        for reason, count in metrics["skippedManualReasonCounts"].items()
    )
    lines.extend([
        "",
        "## Outcomes",
        "",
        "| Outcome | Clusters |",
        "|---|---:|",
    ])
    lines.extend(
        f"| {outcome} | {count} |"
        for outcome, count in metrics["outcomeCounts"].items()
    )
    lines.extend([
        "",
        "## Local tools the model was given",
        "",
        "| Tool | Evidence |",
        "|---|---|",
    ])
    lines.extend(
        f"| `{tool['name']}` | {tool['description']} |"
        for tool in document["toolManifest"]
    )
    lines.extend([
        "",
        "No merchant descriptions are included. The merchant text was sent "
        "only to the loopback model endpoint above and was never written to "
        "disk, logged, or sent to any other destination.",
        "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def summarize_agent(document: dict[str, Any]) -> str:
    """A PII-free one-line summary safe to print to a terminal."""
    metrics = document["metrics"]
    return (
        f"agent model={document['model'].get('model', '')} "
        f"clusters={metrics['clusterCount']} "
        f"activities={metrics['activityCount']} "
        f"applicable={metrics['applicableActivityCount']} "
        f"belowThreshold={metrics['belowThresholdCount']} "
        f"abstained={metrics['abstainedCount']} "
        f"cacheHits={metrics['cache'].get('hits', 0)}"
    )
