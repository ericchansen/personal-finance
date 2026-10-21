"""Multinomial naive Bayes with calibrated confidence and readable reasons. Standard library only."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict

ALPHA = 0.5
BINS = 10
STRONG = 3  # a reason seen fewer times than this never outranks better-evidenced ones


def sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x)) if x >= 0 else math.exp(x) / (1 + math.exp(x))


def logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


class NaiveBayes:
    def __init__(self, classes: list[str]):
        self.classes = list(classes)
        self.prior = {c: 0.0 for c in self.classes}
        self.counts = {c: defaultdict(float) for c in self.classes}
        self.seen = {c: defaultdict(int) for c in self.classes}
        self.totals = {c: 0.0 for c in self.classes}
        self.vocab: set[str] = set()
        self.calibration = [1.0, 0.0]  # Platt slope and intercept; identity until fitted

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
        raw posterior, which breaks ties left by rounding the calibrated value."""
        probs = self.raw(found)
        best = max(probs, key=probs.get)
        runner = max((c for c in self.classes if c != best), key=probs.get)
        lift = sorted(
            ((f, self._log_likelihood(best, f) - self._log_likelihood(runner, f), self.seen[best].get(f, 0))
             for f in dict.fromkeys(found) if f in self.vocab),
            key=lambda item: (item[2] >= STRONG, item[1]), reverse=True,
        )
        return best, self.calibrate(probs[best]), [item for item in lift if item[1] > 0 and item[2] > 0][:3], probs[best]

    def calibrate(self, p: float) -> float:
        slope, intercept = self.calibration
        return round(sigmoid(slope * logit(p) + intercept), 3)

    def fit_calibration(self, pairs: list[tuple[float, bool]]) -> None:
        """Platt scaling, sigmoid(a * logit(p) + b), fit on held-out (p, correct) pairs.

        Monotone and continuous, so it keeps the model's ranking and resolution; histogram binning
        collapsed every prediction into a few values. Newton's method with backtracking and Platt's
        smoothed targets, after Lin, Lin and Weng (2007).
        """
        if not pairs:
            return
        right = sum(correct for _, correct in pairs)
        hit, miss = (right + 1) / (right + 2), 1 / (len(pairs) - right + 2)
        data = [(logit(p), hit if correct else miss) for p, correct in pairs]

        def loss(a, b):
            return sum(max(s, 0) + math.log1p(math.exp(-abs(s))) - t * s for z, t in data for s in [a * z + b])

        a, b = 0.0, logit((right + 1) / (len(pairs) + 2))
        current = loss(a, b)
        for _ in range(100):
            ga = gb = 0.0
            haa = hbb = 1e-12
            hab = 0.0
            for z, target in data:
                q = sigmoid(a * z + b)
                d, w = q - target, q * (1 - q)
                ga, gb, haa, hab, hbb = ga + d * z, gb + d, haa + w * z * z, hab + w * z, hbb + w
            if abs(ga) + abs(gb) < 1e-6:
                break
            det = haa * hbb - hab * hab
            da, db = (hbb * ga - hab * gb) / det, (haa * gb - hab * ga) / det
            step, slope = 1.0, ga * da + gb * db
            while step > 1e-10 and loss(a - step * da, b - step * db) > current - 1e-4 * step * slope:
                step /= 2
            if step <= 1e-10:
                break
            a, b = a - step * da, b - step * db
            current = loss(a, b)
        self.calibration = [round(max(a, 1e-3), 6), round(b, 6)]

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
