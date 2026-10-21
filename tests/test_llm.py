"""The local-model categorizer against a fake Ollama and the in-memory Wealthfolio. All data is synthetic."""

import json
import math

import pytest

from labeling import llm
from test_labeling import world  # noqa: F401  (fixture)


def logprobs(content: str, value: str, p: float) -> list[dict]:
    """Tokens for `content`, with the category value split mid-character so byte offsets matter."""
    raw, start = content.encode(), content.index(value)
    head, body = raw[:len(content[:start].encode())], value.encode()
    cut = len(body) // 2
    pieces = [(head[:-3], -2.0), (head[-3:], -1.0), (body[:cut], math.log(p) / 2), (body[cut:], math.log(p) / 2),
              (raw[len(head) + len(body):], -3.0)]
    return [{"token": b.decode(errors="replace"), "bytes": list(b), "logprob": lp} for b, lp in pieces if b]


class FakeOllama(llm.Ollama):
    def __init__(self, decide, tags=None, url="http://127.0.0.1:11434"):
        self.decide, self.requests = decide, []
        self.tags = [{"name": "tiny:1b", "digest": "sha256:0123456789abcdef"}] if tags is None else tags
        super().__init__(url, "tiny:1b")

    def call(self, path, body=None):
        if path == "/api/tags":
            return {"models": self.tags}
        self.requests.append(body)
        category, p = self.decide(json.loads(body["messages"][1]["content"]))
        assert category in body["format"]["properties"]["category"]["enum"]
        content = json.dumps({"category": category, "confidence": 0.8, "rationale": " synthetic reason "}, ensure_ascii=False)
        tokens = logprobs(content, category, p or 0.5)
        return {"message": {"content": content}, "logprobs": tokens if p else tokens[:1]}  # some runners send one token


def decide(facts):
    name = facts["merchant"]
    if "GYM" in name:
        return "none", 0.6
    if "COFFEE" in name or "KETTLE" in name:
        return "Food & Dining › Coffee", 0.9
    if "HARDWARE" in name:
        return "Groceries", 0.7
    if "CORNER" in name:
        return "Groceries", None
    return "Food & Dining", 0.5


def test_client_stays_on_this_machine():
    with pytest.raises(ValueError, match="non-loopback"):
        FakeOllama(decide, url="http://192.0.2.5:11434")
    with pytest.raises(ValueError, match="cloud"):
        FakeOllama(decide, tags=[{"name": "tiny:1b", "digest": "sha256:00", "remote_host": "https://ollama.example"}])
    with pytest.raises(ValueError, match="not pulled"):
        FakeOllama(decide, tags=[])


def test_probability_counts_only_the_category_value():
    content = json.dumps({"category": "Food & Dining › Coffee", "confidence": 1, "rationale": "x"}, ensure_ascii=False)
    tokens = logprobs(content, "Food & Dining › Coffee", 0.64)
    assert llm.probability(tokens, content) == pytest.approx(0.64)
    assert llm.probability(tokens[:1], content) is None and llm.probability(None, content) is None


def test_proposals_are_typed_cached_and_suggested(world):  # noqa: F811
    fake, ws = world
    fake.add("xfer", "checking", "ONLINE TRANSFER TO SAVINGS", amount=500, activityType="TRANSFER_OUT")
    ws.refresh()
    assert llm.choices(ws) == {"Food & Dining": "cat_food", "Food & Dining › Coffee": "cat_food_coffee",
                               "Groceries": "cat_groceries", "none": None}
    assert "Synthetic" not in json.dumps(llm.card(ws, "HARDWARE STORE"))  # account names never reach the model

    client = FakeOllama(decide)
    result = llm.run(ws, client, evaluate_only=False, limit=None, log=lambda _: None)
    asked = {json.loads(r["messages"][1]["content"])["merchant"] for r in client.requests}
    assert "ONLINE TRANSFER" not in asked and "HARDWARE STORE" in asked
    assert result["answeredNone"] == 3 and result["threshold"] is None
    found = llm.proposals(ws)
    assert found["RIVER GYM"]["categoryId"] is None
    hardware = found["HARDWARE STORE"]
    assert (hardware["categoryId"], hardware["rationale"], hardware["confidence"]) == ("cat_groceries", "synthetic reason", None)
    assert hardware["p"] == pytest.approx(0.7) and hardware["model"] == "tiny:1b" and hardware["digest"] == "0123456789ab"
    assert found["CORNER SHOP"]["p"] == found["CORNER SHOP"]["said"] == 0.8  # no full log probabilities

    llm.run(ws, client, evaluate_only=False, limit=None, log=lambda _: None)
    assert len(client.requests) == len(asked)  # unchanged merchants are answered from the file

    ids = [i for i, key in ws.merchant.items() if key == "HARDWARE STORE"]
    assert ws.suggest_category(ids, "Need") == ("cat_groceries", "local model tiny:1b: synthetic reason")
    assert not ws.suggest_category(ids, "Want")[1].startswith("local model")


def test_evaluation_scores_known_merchants_and_calibrates(world):  # noqa: F811
    fake, ws = world
    ws.categorize("cat_food_coffee", ids=[i for i, key in ws.merchant.items() if key == "BLUE KETTLE"])
    ws.categorize("cat_groceries", ids=[i for i, key in ws.merchant.items() if key == "HARDWARE STORE"])
    truth = llm.known(ws)
    assert truth == {"BLUE KETTLE": ("cat_food_coffee", "manual"), "HARDWARE STORE": ("cat_groceries", "manual")}

    report = llm.run(ws, FakeOllama(decide), evaluate_only=True, limit=None, log=lambda _: None)
    assert report["all"]["n"] == 2 and report["all"]["accuracy"] == 1.0 and report["threshold"] is None
    assert llm.latest_eval(ws, "tiny:1b")["calibration"] == report["calibration"]
    assert llm.proposals(ws)["HARDWARE STORE"]["confidence"] is not None


def test_threshold_needs_held_out_precision_on_hand_set_categories():
    truth = {f"M{i}": (f"cat_{i % 3}", "manual") for i in range(200)}
    right = [{"merchant": f"M{i}", "p": 0.99, "categoryId": f"cat_{i % 3}", "seconds": 1} for i in range(120)]
    unsure = [{"merchant": f"M{i}", "p": 0.4, "categoryId": "cat_x", "seconds": 1} for i in range(120, 200)]
    report = llm.evaluate(right + unsure, truth)
    assert report["threshold"] is not None
    assert report["all"]["atThreshold"][str(report["threshold"])]["precision"] >= 0.95
    wrong = [{**a, "categoryId": "cat_x"} for a in right]
    assert llm.evaluate(wrong + unsure, truth)["threshold"] is None
    by_rule = {key: (category, "rule") for key, (category, _) in truth.items()}
    assert llm.evaluate(right + unsure, by_rule)["threshold"] is None  # agreement with rules certifies nothing
