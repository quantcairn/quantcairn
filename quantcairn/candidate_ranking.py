"""Public API: candidate ranking and score polishing."""
import importlib as _il; _m = _il.import_module("src.openalpha.candidate_ranking")
score_candidate = _m.score_candidate
score_candidates = _m.score_candidates
__all__ = ["score_candidate", "score_candidates"]
