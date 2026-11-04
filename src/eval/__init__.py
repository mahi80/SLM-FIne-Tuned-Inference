"""Golden-set scoring and release gates for the triage contract."""

from .gate import evaluate_gate, load_gate_config
from .scoring import score, summarise

__all__ = ["evaluate_gate", "load_gate_config", "score", "summarise"]
