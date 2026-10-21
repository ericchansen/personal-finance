"""Multinomial naive Bayes with calibrated confidence and readable reasons. Standard library only."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict

ALPHA = 0.5
BINS = 10


class NaiveBayes:
    def __init__(self, classes: list[str]):
        self.classes = list(classes)
        self.prior = {c: 0.0 for c in self.classes}
        self.counts = {c: defaultdict(float) for c in self.classes}
        self.seen = {c: defaultdict(int) for c in self.classes}
        self.totals = {c: 0.0 for c in self.classes}
        self.vocab: set[str] = set()
        self.calibration = [None] * BINS

    def fit(self, examples: list[tuple[list[str], str, float]]) -> "NaiveBayes":
        for found, label, weight in examples:
            self.prior[label] += weight
            for feature in found:
                self.counts[label][feature] += weight
                self.seen[label][feature] += 1
                self.totals[label] += weight
                self.vocab.add(feature)
        return self

    def _log_likelihood(self, label: str, feature: str) -> float:
        return math.log((self.counts[label].get(feature, 0.0) + ALPHA) / (self.totals[label] + ALPHA * (len(self.vocab) + 1)))

    def raw(self, found: list[str]) -> dict[str, float]:
        total = sum(self.prior.values())
        known = [f for f in found if f in self.vocab]
        scores = {
            c: math.log((self.prior[c] + 1) / (total + len(self.classes)))
            + sum(self._log_likelihood(c, f) for f in known)
            for c in self.classes
        }
        top = max(scores.values())
        exp = {c: math.exp(s - top) for c, s in scores.items()}
        norm = sum(exp.values())
        return {c: v / norm for c, v in exp.items()}

    def predict(self, found: list[str]) -> tuple[str, float, list[tuple[str, float, int]], float]:
        """Best class, calibrated confidence, the features that favoured it most (with times seen), and the
        raw posterior. Calibration is monotone but coarse, so the raw posterior breaks its ties when ranking."""
        probs = self.raw(found)
        best = max(probs, key=probs.get)
        runner = max((c for c in self.classes if c != best), key=probs.get)
        lift = sorted(
            ((f, self._log_likelihood(best, f) - self._log_likelihood(runner, f), self.seen[best].get(f, 0))
             for f in dict.fromkeys(found) if f in self.vocab),
            key=lambda item: -item[1],
        )
        return best, self.calibrate(probs[best]), [item for item in lift if item[1] > 0 and item[2] > 0][:3], probs[best]

    def calibrate(self, p: float) -> float:
        value = self.calibration[min(int(p * BINS), BINS - 1)]
        return round(p if value is None else value, 3)

    def fit_calibration(self, pairs: list[tuple[float, bool]]) -> None:
        """Histogram binning made monotone with pool-adjacent-violators."""
        hits, sizes = [0.0] * BINS, [0.0] * BINS
        for p, correct in pairs:
            b = min(int(p * BINS), BINS - 1)
            hits[b] += correct
            sizes[b] += 1
        blocks = [[(hits[b] + 1) / (sizes[b] + 2), sizes[b] + 2, [b]] for b in range(BINS)]
        merged = []
        for block in blocks:
            merged.append(block)
            while len(merged) > 1 and merged[-2][0] > merged[-1][0]:
                right, left = merged.pop(), merged.pop()
                weight = left[1] + right[1]
                merged.append([(left[0] * left[1] + right[0] * right[1]) / weight, weight, left[2] + right[2]])
        for value, _, members in merged:
            for b in members:
                self.calibration[b] = round(value, 4)

    def to_json(self) -> dict:
        return {
            "classes": self.classes, "prior": self.prior, "totals": self.totals,
            "counts": {c: dict(v) for c, v in self.counts.items()}, "calibration": self.calibration,
        }


def version(examples: list[tuple[str, list[str], str, float]]) -> str:
    digest = hashlib.sha1(json.dumps(sorted((i, l, w) for i, _, l, w in examples)).encode())
    return digest.hexdigest()[:10]


def folds(examples, k=5):
    """Split by merchant so every held-out merchant is unseen, as unlabeled merchants are."""
    by_fold = defaultdict(list)
    for example in examples:
        merchant = next((f for f in example[1] if f.startswith("m:")), example[0])
        by_fold[int(hashlib.sha1(merchant.encode()).hexdigest(), 16) % k].append(example)
    return [by_fold[i] for i in range(k)]


def out_of_fold(classes, examples) -> list[tuple[dict, str]]:
    """(raw probabilities, truth) for every example, predicted by a model that never saw its merchant."""
    result = []
    parts = folds(examples)
    for i, held in enumerate(parts):
        model = NaiveBayes(classes).fit([(f, l, w) for j, p in enumerate(parts) if j != i for _, f, l, w in p])
        result += [(model.raw(f), l) for _, f, l, _ in held]
    return result


def train(classes: list[str], examples: list[tuple[str, list[str], str, float]]) -> tuple[NaiveBayes, str]:
    """`examples` are (activity id, features, label, weight)."""
    model = NaiveBayes(classes).fit([(f, l, w) for _, f, l, w in examples])
    held = out_of_fold(classes, examples) if len({l for _, _, l, _ in examples}) > 1 else []
    model.fit_calibration([(max(p.values()), max(p, key=p.get) == truth) for p, truth in held])
    return model, version(examples)


def metrics(pairs: list[tuple[str, float, str]], threshold: float) -> dict:
    """`pairs` are (predicted, calibrated confidence, truth)."""
    if not pairs:
        return {"n": 0}
    sure = [(p, t) for p, c, t in pairs if c >= threshold]
    gaps = defaultdict(lambda: [0.0, 0.0, 0])
    for p, c, t in pairs:
        gap = gaps[min(int(c * BINS), BINS - 1)]
        gap[0] += c
        gap[1] += p == t
        gap[2] += 1
    return {
        "n": len(pairs),
        "accuracy": round(sum(p == t for p, _, t in pairs) / len(pairs), 3),
        "coverage": round(len(sure) / len(pairs), 3),
        "precisionAtThreshold": round(sum(p == t for p, t in sure) / len(sure), 3) if sure else None,
        "calibrationError": round(sum(abs(c - h) for c, h, _ in gaps.values()) / len(pairs), 3),
    }
