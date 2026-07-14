from __future__ import annotations

import json
from dataclasses import dataclass

import numpy as np


LABELS = ("up", "flat", "down")


@dataclass
class ModelEvaluation:
    samples: int
    brier_score: float
    log_loss: float
    accuracy: float
    baseline_brier: float
    baseline_log_loss: float

    def as_dict(self) -> dict:
        return {
            "samples": self.samples,
            "brier_score": self.brier_score,
            "log_loss": self.log_loss,
            "accuracy": self.accuracy,
            "baseline_brier": self.baseline_brier,
            "baseline_log_loss": self.baseline_log_loss,
        }


class OnlineSoftmaxModel:
    """Small serializable multinomial logistic model with no hidden ML dependency."""

    def __init__(self, feature_names: list[str]):
        self.feature_names = list(feature_names)
        self.mean = np.zeros(len(feature_names), dtype=float)
        self.scale = np.ones(len(feature_names), dtype=float)
        self.weights = np.zeros((len(LABELS), len(feature_names) + 1), dtype=float)
        self.class_prior = np.full(len(LABELS), 1 / len(LABELS), dtype=float)
        self.prior_blend = 0.0

    def fit(
        self,
        x: np.ndarray,
        y: np.ndarray,
        epochs: int = 600,
        learning_rate: float = 0.08,
        l2: float = 0.002,
    ) -> "OnlineSoftmaxModel":
        if len(x) != len(y) or len(x) < 2:
            raise ValueError("training requires matching non-trivial X and y")
        self.mean = x.mean(axis=0)
        self.scale = x.std(axis=0)
        self.scale[self.scale < 1e-8] = 1.0
        normalized = (x - self.mean) / self.scale
        design = np.column_stack([np.ones(len(normalized)), normalized])
        counts = np.bincount(y, minlength=len(LABELS)).astype(float)
        self.class_prior = (counts + 1) / (counts.sum() + len(LABELS))
        sample_weights = len(y) / (len(LABELS) * np.maximum(counts, 1))
        one_hot = np.eye(len(LABELS))[y]
        for epoch in range(epochs):
            probabilities = _softmax(design @ self.weights.T)
            weighted_error = (probabilities - one_hot) * sample_weights[y, None]
            gradient = weighted_error.T @ design / len(design)
            gradient[:, 1:] += l2 * self.weights[:, 1:]
            rate = learning_rate / np.sqrt(1 + epoch / 100)
            self.weights -= rate * gradient
        return self

    def predict_proba(self, x: np.ndarray) -> np.ndarray:
        matrix = np.atleast_2d(x).astype(float)
        normalized = (matrix - self.mean) / self.scale
        design = np.column_stack([np.ones(len(normalized)), normalized])
        probabilities = _softmax(design @ self.weights.T)
        if self.prior_blend > 0:
            probabilities = (
                1 - self.prior_blend
            ) * probabilities + self.prior_blend * self.class_prior
        return probabilities

    def evaluate(self, x: np.ndarray, y: np.ndarray) -> ModelEvaluation:
        probabilities = self.predict_proba(x)
        actual = np.eye(len(LABELS))[y]
        brier = float(np.mean(np.sum((probabilities - actual) ** 2, axis=1) / len(LABELS)))
        log_loss = float(-np.mean(np.log(np.clip(probabilities[np.arange(len(y)), y], 1e-12, 1))))
        accuracy = float(np.mean(np.argmax(probabilities, axis=1) == y))
        baseline = np.tile(self.class_prior, (len(y), 1))
        baseline_brier = float(np.mean(np.sum((baseline - actual) ** 2, axis=1) / len(LABELS)))
        baseline_log_loss = float(
            -np.mean(np.log(np.clip(baseline[np.arange(len(y)), y], 1e-12, 1)))
        )
        return ModelEvaluation(
            samples=len(y),
            brier_score=brier,
            log_loss=log_loss,
            accuracy=accuracy,
            baseline_brier=baseline_brier,
            baseline_log_loss=baseline_log_loss,
        )

    def dumps(self) -> str:
        return json.dumps(
            {
                "feature_names": self.feature_names,
                "mean": self.mean.tolist(),
                "scale": self.scale.tolist(),
                "weights": self.weights.tolist(),
                "class_prior": self.class_prior.tolist(),
                "prior_blend": self.prior_blend,
            }
        )

    def feature_importance(self, top_n: int = 10) -> dict[str, list[dict]]:
        output = {}
        for class_index, label in enumerate(LABELS):
            coefficients = self.weights[class_index, 1:]
            indices = np.argsort(np.abs(coefficients))[::-1][:top_n]
            output[label] = [
                {
                    "feature": self.feature_names[index],
                    "coefficient": float(coefficients[index]),
                }
                for index in indices
            ]
        return output

    @classmethod
    def loads(cls, payload: str) -> "OnlineSoftmaxModel":
        data = json.loads(payload)
        model = cls(data["feature_names"])
        model.mean = np.asarray(data["mean"], dtype=float)
        model.scale = np.asarray(data["scale"], dtype=float)
        model.weights = np.asarray(data["weights"], dtype=float)
        model.class_prior = np.asarray(data["class_prior"], dtype=float)
        model.prior_blend = float(data.get("prior_blend", 0.0))
        return model


def label_index(label: str) -> int:
    return LABELS.index(label)


def _softmax(logits: np.ndarray) -> np.ndarray:
    shifted = logits - logits.max(axis=1, keepdims=True)
    exponentials = np.exp(shifted)
    return exponentials / exponentials.sum(axis=1, keepdims=True)
