"""Categorize merchants with a local Ollama model: a forced choice plus its probability.

TypeSafe AI's Jev answers typed questions with a choice and a probability, but it runs in the cloud. This
does the same on a local model. The JSON schema allows only existing Need or Want categories (or "none"),
and confidence is the model's own probability of the category it chose, read from token log probabilities
and then calibrated on merchants whose category is already known. Loopback only: nothing leaves the machine.
"""

from __future__ import annotations

import functools
import hashlib
import json
import math
import statistics
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict

from labeling import model as nb
from labeling.ledger import now

NONE = "none"
PROMPT = (
    "You categorize household card and bank transactions. Given one merchant's transactions as JSON, choose "
    "its spending category from the allowed list: the most specific one that fits, a subcategory rather than "
    "its parent. Answer \"none\" only for transfers, card or loan payments, merchants you cannot identify, and "
    "stores whose purchases clearly span unrelated categories. Then give your confidence, from 0 to 1, that "
    "the category is right, and a rationale under 20 words.\n"
    "Allowed categories:\n"
)
PROMPT_VERSION = hashlib.sha1(PROMPT.encode()).hexdigest()[:8]
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
PRECISION, MIN_COVERED = 0.95, 30  # auto-apply only where held-out precision is at least this, on this many


class Ollama:
    """A loopback-only Ollama client that refuses cloud models."""

    def __init__(self, base_url: str, model: str, timeout: float = 600):
        if urllib.parse.urlsplit(base_url).hostname not in LOOPBACK:
            raise ValueError(f"refusing a non-loopback Ollama URL: {base_url}")
        self.base_url, self.model, self.timeout = base_url.rstrip("/"), model, timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        tag = next((m for m in self.call("/api/tags")["models"] if m["name"] == model), None)
        if tag is None:
            raise ValueError(f"{model} is not pulled in Ollama")
        if tag.get("remote_host") or tag.get("remote_model"):
            raise ValueError(f"refusing {model}: it runs in the cloud")
        self.digest = tag["digest"].removeprefix("sha256:")[:12]

    def call(self, path: str, body: dict | None = None) -> dict:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base_url + path, data, {"Content-Type": "application/json"})
        with self.opener.open(request, timeout=self.timeout) as response:
            return json.load(response)


def choices(ws) -> dict[str, str | None]:
    """The allowed answers, `Parent › Child` names of every Need or Want category, mapped to category IDs."""
    found = {}
    for category_id, category in ws.categories.items():
        if ws.need_want({"categoryIds": [category_id]}):
            parent = ws.categories.get(category.get("parentId") or "")
            found[f"{parent['name']} › {category['name']}" if parent else category["name"]] = category_id
    return {**dict(sorted(found.items())), NONE: None}


def card(ws, key: str) -> dict:
    """What the model sees about a merchant: no dates, account names, numbers, or existing categories."""
    rows = [ws.rows[i] for i, k in ws.merchant.items() if k == key]
    raw = [ws.raw.get(r.get("idempotencyKey") or "", {}) for r in rows]
    top = lambda values: [v for v, _ in Counter(v for v in values if v).most_common(5)]
    return {
        "merchant": ws.names.get(key, key),
        "descriptions": top(r.get("notes") for r in rows),
        "payees": top(x.get("payee") for x in raw),
        "mcc": top(str(x["mcc"]) for x in raw if x.get("mcc")),
        "transactions": len(rows),
        "typicalAmount": round(statistics.median(abs(float(r.get("amount") or 0)) for r in rows), 2),
        "accountTypes": top(ws.accounts.get(r["accountId"], {}).get("accountType") for r in rows),
        "activityTypes": top(r["activityType"] for r in rows),
    }


def probability(logprobs: list[dict] | None, content: str) -> float | None:
    """The model's probability of the category it wrote: the product over the tokens of that JSON value.

    None unless the log probabilities cover the whole reply (some Ollama runners return only the first token).
    """
    widths = [len(bytes(t["bytes"]) if t.get("bytes") else t["token"].encode()) for t in logprobs or []]
    if sum(widths) != len(content.encode()):
        return None
    quote = content.index('"', content.index(":", content.index('"category"')))
    end = json.JSONDecoder().raw_decode(content, quote)[1]
    start, stop = len(content[:quote + 1].encode()), len(content[:end - 1].encode())
    total, offset = 0.0, 0
    for token, width in zip(logprobs, widths):
        if offset < stop and offset + width > start:
            total += token["logprob"]
        offset += width
    return math.exp(total)


def ask(client: Ollama, ws, key: str, allowed: dict[str, str | None]) -> dict:
    """A typed answer. Its score `p` is the probability from token log probabilities when the runner returns
    them for the whole reply (they rank answers best), else the model's self-reported confidence."""
    facts = card(ws, key)
    schema = {"type": "object", "required": ["category", "confidence", "rationale"], "properties": {
        "category": {"type": "string", "enum": list(allowed)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "rationale": {"type": "string", "maxLength": 160},
    }}
    started = time.perf_counter()
    reply = client.call("/api/chat", {
        "model": client.model, "stream": False, "think": False, "format": schema, "logprobs": True,
        "options": {"temperature": 0}, "keep_alive": "10m",
        "messages": [{"role": "system", "content": PROMPT + "\n".join(allowed)},
                     {"role": "user", "content": json.dumps(facts)}],
    })
    content = reply["message"]["content"]
    answer = json.loads(content)
    said = min(max(float(answer["confidence"]), 0.0), 1.0)
    logprob = probability(reply.get("logprobs"), content)
    return {
        "merchant": key, "categoryId": allowed[answer["category"]], "category": answer["category"],
        "p": said if logprob is None else logprob, "said": said, "rationale": answer["rationale"].strip(),
        "model": client.model, "digest": client.digest, "prompt": PROMPT_VERSION, "facts": fingerprint(facts, allowed),
        "seconds": round(time.perf_counter() - started, 2), "at": now(),
    }


def fingerprint(facts: dict, allowed: dict) -> str:
    return hashlib.sha1(json.dumps([facts, list(allowed)], sort_keys=True).encode()).hexdigest()[:10]


def transfers(ws) -> set[str]:
    return {ws.merchant[i] for i, r in ws.rows.items() if r["activityType"].startswith("TRANSFER")}


def candidates(ws) -> list[str]:
    """Merchants with uncategorized spending rows and no transfers, by dollars uncategorized."""
    dollars, skip = Counter(), transfers(ws)
    for row_id, row in ws.rows.items():
        key = ws.merchant[row_id]
        if key and key not in skip and not row["categoryIds"] and row.get("visibleSpendingAmount", 1) != 0:
            dollars[key] += abs(float(row.get("amount") or 0))
    return [key for key, _ in dollars.most_common()]


def known(ws) -> dict[str, tuple[str | None, str]]:
    """Merchants whose rows all share one category: (the right answer, 'manual' or 'rule').

    A category outside Need and Want (transfers and the like) makes "none" the right answer.
    """
    found: dict[str, set] = defaultdict(set)
    skip = transfers(ws)
    for row_id, row in ws.rows.items():
        key = ws.merchant[row_id]
        if key and key not in skip:
            found[key].update((a["categoryId"], a.get("source") or "manual") for a in row["assignments"]
                              if a["taxonomyId"] == "spending_categories")
            found[key].update((s["categoryId"], "split") for s in row["splits"] if s["taxonomyId"] == "spending_categories")
    allowed = set(choices(ws).values())
    truth = {}
    for key, pairs in found.items():
        categories = {c for c, _ in pairs}
        if len(categories) == 1 and "split" not in {s for _, s in pairs}:
            category = categories.pop()
            truth[key] = (category if category in allowed else None, "manual" if ("manual" in {s for _, s in pairs}) else "rule")
    return truth


class Answers:
    """Every answer, appended privately to <data>/labels/llm-proposals.jsonl; the newest per merchant wins."""

    def __init__(self, ws):
        self.path = ws.dir / "llm-proposals.jsonl"
        lines = self.path.read_text(encoding="utf-8").splitlines() if self.path.exists() else []
        self.latest = {(e["merchant"], e["model"]): e for e in map(json.loads, filter(None, lines))}

    def get(self, ws, client: Ollama, key: str, allowed: dict) -> dict:
        cached = self.latest.get((key, client.model))
        if cached and (cached["digest"], cached["prompt"], cached["facts"]) == (
                client.digest, PROMPT_VERSION, fingerprint(card(ws, key), allowed)):
            return cached
        answer = ask(client, ws, key, allowed)
        answer["confidence"] = confidence(ws, answer)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(answer, sort_keys=True) + "\n")
        self.latest[(key, client.model)] = answer
        return answer


def latest_eval(ws, model: str) -> dict | None:
    path = ws.dir / f"llm-eval-{model.replace(':', '_').replace('/', '_')}.json"
    return read_json(path, path.stat().st_mtime_ns) if path.exists() else None


@functools.lru_cache(maxsize=16)
def read_json(path, _stamp: int) -> dict:
    """Parsed once per version of the file; every proposal's confidence needs its model's report."""
    return json.loads(path.read_text(encoding="utf-8"))


def confidence(ws, answer: dict) -> float | None:
    """Calibrated by the latest evaluation of the same model, digest and prompt; None until there is one."""
    report = latest_eval(ws, answer["model"])
    if answer.get("p") is None or not report or (report["digest"], report["prompt"]) != (answer["digest"], answer["prompt"]):
        return None
    scaler = nb.NaiveBayes([])
    scaler.calibration = report["calibration"]
    return scaler.calibrate(answer["p"])


def proposals(ws) -> dict[str, dict]:
    """The newest calibrated answer per merchant, across models (a later answer replaces an earlier one)."""
    if not (ws.dir / "llm-proposals.jsonl").exists():
        return {}
    found = {}
    for (key, _), answer in sorted(Answers(ws).latest.items(), key=lambda item: item[1]["at"]):
        found[key] = {**answer, "confidence": confidence(ws, answer)}
    return found


def coverage(pairs: list[tuple[str | None, float, str | None]], threshold: float) -> dict:
    """Of category answers (never "none") at or above the threshold: how many, and how many were right."""
    sure = [(p, t) for p, c, t in pairs if p and c >= threshold]
    return {"covered": len(sure), "coverage": round(len(sure) / len(pairs), 3) if pairs else None,
            "precision": round(sum(p == t for p, t in sure) / len(sure), 3) if sure else None}


def evaluate(answers: list[dict], truth: dict[str, tuple[str | None, str]]) -> dict:
    """Accuracy and calibration on known merchants, each scored by a calibration fit without it."""
    rows = [(a["merchant"], a["p"], a["categoryId"], *truth[a["merchant"]]) for a in answers
            if a["merchant"] in truth and a.get("p") is not None]
    folds = defaultdict(list)
    for row in rows:
        folds[int(hashlib.sha1(row[0].encode()).hexdigest(), 16) % 5].append(row)
    held = []
    for i, part in folds.items():
        scaler = nb.NaiveBayes([])
        scaler.fit_calibration([(p, pred == t) for j, other in folds.items() if j != i for _, p, pred, t, _ in other])
        held += [(pred, scaler.calibrate(p), t, source) for _, p, pred, t, source in part]
    scaler = nb.NaiveBayes([])
    scaler.fit_calibration([(p, pred == t) for _, p, pred, t, _ in rows])
    # Only hand-set categories certify a threshold: rule-set ones mostly come from another model's judgement,
    # and the merchants left uncategorized are the ones it found hard.
    pairs = [(p, c, t) for p, c, t, source in held if source == "manual"]
    threshold = next((x / 100 for x in range(50, 100) if (lambda s: s["covered"] >= MIN_COVERED
                      and s["precision"] >= PRECISION)(coverage(pairs, x / 100))), None)
    model = answers[0] if answers else {}
    report = {
        "model": model.get("model"), "digest": model.get("digest"), "prompt": model.get("prompt"), "at": now(),
        "calibration": scaler.calibration, "threshold": threshold,
        "secondsPerMerchant": round(statistics.median(a["seconds"] for a in answers), 2) if answers else None,
        "note": "zero-shot, so every merchant is unseen; confidence is calibrated on the other folds; "
                "the threshold is certified on hand-set (manual) categories only",
    }
    for source in ("all", "manual", "rule"):
        subset = [(p, c, t) for p, c, t, s in held if source in ("all", s)]
        report[source] = {
            **nb.metrics(subset, threshold or PRECISION),
            "answeredNone": sum(p is None for p, _, _ in subset),
            "atThreshold": {str(x): coverage(subset, x) for x in (0.7, 0.8, 0.9, 0.95, *([threshold] if threshold else []))},
        }
    return report


def run(ws, client: Ollama, *, evaluate_only: bool, limit: int | None, log=print) -> dict:
    allowed, answers = choices(ws), Answers(ws)
    truth = known(ws)
    if evaluate_only:
        # Every hand-categorized merchant first, then rule-categorized ones in a stable, arbitrary order.
        keys = sorted(truth, key=lambda k: (truth[k][1] != "manual", hashlib.sha1(k.encode()).hexdigest()))
    else:
        keys = candidates(ws)
    found = []
    for n, key in enumerate(keys[:limit], 1):
        found.append(answers.get(ws, client, key, allowed))
        if n % 25 == 0:
            log(f"{n}/{len(keys[:limit])} merchants")
    if not evaluate_only:
        threshold = (latest_eval(ws, client.model) or {}).get("threshold")
        return {"merchants": len(found), "answeredNone": sum(not a["categoryId"] for a in found), "threshold": threshold,
                "aboveThreshold": sum(1 for a in found if a["categoryId"] and threshold and (confidence(ws, a) or 0) >= threshold)}
    report = evaluate(found, truth)
    path = ws.dir / f"llm-eval-{client.model.replace(':', '_').replace('/', '_')}.json"
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report
