"""Regression tests: funnel invariant — no stage may output more candidates than it received.

These guard against the FORMAL_TOP backfill bug where candidates that never passed
DATA_QUALITY were injected into the final output, causing output_count > input_count.
"""

from __future__ import annotations

from pathlib import Path

from src.openalpha.funnel_tracker import FunnelTracker


class TestFunnelInvariantOutputLeInput:
    """Every stage must satisfy: output_count <= input_count."""

    # ── Core invariant ──────────────────────────────────────────────────────

    def test_no_stage_can_output_more_than_input(self, tmp_path: Path):
        """The universal funnel invariant: output ≤ input for every stage."""
        tracker = FunnelTracker(selection_run_id="invariant-1", selection_date="2026-07-24",
                                project_dir=tmp_path)
        tracker.add_stage("UNIVERSE", ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"],
                          ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"])
        tracker.add_stage("UNIVERSE_FILTER", ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"],
                          ["A", "B", "C", "D", "E", "F"])
        tracker.add_stage("MARKET_DATA", ["A", "B", "C", "D", "E", "F"],
                          ["A", "B", "C"])
        tracker.add_stage("SCORING_ELIGIBLE", ["A", "B", "C"],
                          ["A", "B", "C"])
        tracker.add_stage("BASE_RANKING", ["A", "B", "C"],
                          ["A", "B", "C"])
        tracker.add_stage("FORMAL_ELIGIBILITY", ["A", "B", "C"],
                          ["A", "B", "C"])
        tracker.add_stage("COMPOSITION_FILTER", ["A", "B", "C"],
                          ["A", "B", "C"])
        tracker.add_stage("DATA_QUALITY", ["A", "B", "C"],
                          ["A", "B"])
        tracker.add_stage("FORMAL_TOP", ["A", "B"],
                          ["A", "B"])

        for rec in tracker.records:
            assert rec.output_count <= rec.input_count, (
                f"Stage {rec.stage}: output={rec.output_count} > input={rec.input_count}"
            )
        assert tracker.validate()["consistent"] is True

    # ── FORMAL_TOP regression: the specific 2→3 injection bug ───────────────

    def test_formal_top_output_must_not_exceed_data_quality_output(self, tmp_path: Path):
        """Regression: DATA_QUALITY outputs 2 → FORMAL_TOP must output ≤ 2.
        The old backfill loop padded from the raw scored list, causing output=3 > input=2."""
        tracker = FunnelTracker(selection_run_id="regression-data-quality-2", selection_date="2026-07-24",
                                project_dir=tmp_path)
        # Full pipeline mimicking the pre-market scenario that triggered the bug
        tracker.add_stage("UNIVERSE", ["A", "B", "C", "D", "E"], ["A", "B", "C", "D", "E"])
        tracker.add_stage("UNIVERSE_FILTER", ["A", "B", "C", "D", "E"], ["A", "B", "C", "D", "E"])
        tracker.add_stage("MARKET_DATA", ["A", "B", "C", "D", "E"], ["A", "B", "C"])
        tracker.add_stage("SCORING_ELIGIBLE", ["A", "B", "C"], ["A", "B", "C"])
        tracker.add_stage("BASE_RANKING", ["A", "B", "C"], ["A", "B", "C"])
        tracker.add_stage("FORMAL_ELIGIBILITY", ["A", "B", "C"], ["A", "B", "C"])
        tracker.add_stage("COMPOSITION_FILTER", ["A", "B", "C"], ["A", "B", "C"])
        tracker.add_stage("DATA_QUALITY", ["A", "B", "C"], ["A", "B"])  # 3→2
        tracker.add_stage("FORMAL_TOP", ["A", "B"], ["A", "B"])  # must be 2→2, not 2→3

        formal_top = tracker.records[-1]
        assert formal_top.output_count == 2, (
            f"FORMAL_TOP output={formal_top.output_count}, expected ≤ 2"
        )
        assert formal_top.output_count <= formal_top.input_count
        assert tracker.validate()["consistent"] is True

    def test_formal_top_single_quality_pass_no_backfill(self, tmp_path: Path):
        """When only 1 candidate passes quality, FORMAL_TOP must output exactly 1 — no padding."""
        tracker = FunnelTracker(selection_run_id="single-pass", selection_date="2026-07-24",
                                project_dir=tmp_path)
        tracker.add_stage("UNIVERSE", ["A", "B", "C", "D", "E"], ["A", "B", "C", "D", "E"])
        tracker.add_stage("UNIVERSE_FILTER", ["A", "B", "C", "D", "E"], ["A", "B", "C"])
        tracker.add_stage("MARKET_DATA", ["A", "B", "C"], ["A", "B", "C"])
        tracker.add_stage("DATA_QUALITY", ["A", "B", "C"], ["A"])  # Only A passes
        tracker.add_stage("SCORING_ELIGIBLE", ["A"], ["A"])
        tracker.add_stage("BASE_RANKING", ["A"], ["A"])
        tracker.add_stage("FORMAL_ELIGIBILITY", ["A"], ["A"])
        tracker.add_stage("COMPOSITION_FILTER", ["A"], ["A"])
        tracker.add_stage("FORMAL_TOP", ["A"], ["A"])  # 1→1, not 1→3

        assert tracker.records[-1].output_count == 1
        assert tracker.records[-1].output_count <= tracker.records[-1].input_count
        assert tracker.validate()["consistent"] is True

    # ── Output > input detection ────────────────────────────────────────────

    def test_output_gt_input_produces_warn_status(self, tmp_path: Path):
        """A stage with output > input gets WARN status and invalidates the pipeline."""
        tracker = FunnelTracker(selection_run_id="warn-detection", selection_date="2026-07-24",
                                project_dir=tmp_path)
        tracker.add_stage("UNIVERSE", ["A", "B"], ["A", "B"])
        tracker.add_stage("DATA_QUALITY", ["A", "B"], ["A"])
        # Deliberately violate the invariant (simulating the old bug)
        tracker.add_stage("FORMAL_TOP", ["A"], ["A", "B", "C"])

        formal_top = tracker.records[-1]
        assert formal_top.status == "WARN"
        assert formal_top.output_count > formal_top.input_count

        validation = tracker.validate()
        assert validation["consistent"] is False
        output_gt_input_warnings = [w for w in validation["warnings"] if w["check"] == "output_gt_input"]
        assert len(output_gt_input_warnings) >= 1

    def test_chain_break_between_stages_still_detected(self, tmp_path: Path):
        """Real chain breaks (not quality fallback) are still reported."""
        tracker = FunnelTracker(selection_run_id="chain-break", selection_date="2026-07-24",
                                project_dir=tmp_path)
        tracker.add_stage("UNIVERSE", ["A", "B", "C"], ["A", "B", "C"])
        tracker.add_stage("UNIVERSE_FILTER", ["A", "B", "C"], ["A", "B"])  # 3→2
        tracker.add_stage("MARKET_DATA", ["A", "B", "C"], ["A", "B", "C"])  # 3→3, chain break!

        validation = tracker.validate()
        assert validation["consistent"] is False
        assert any(w["check"] == "chain_break" for w in validation["warnings"])

    # ── Quality fallback preserves invariant semantics ──────────────────────

    def test_quality_fallback_formal_empty_preview_populated(self, tmp_path: Path):
        """When quality rejects all, fallback sets formal=empty, preview=research-only.
        The DATA_QUALITY stage still satisfies output ≤ input (0 ≤ N)."""
        tracker = FunnelTracker(selection_run_id="fallback-invariant", selection_date="2026-07-24",
                                project_dir=tmp_path)
        tracker.add_stage("UNIVERSE", ["A", "B", "C"], ["A", "B", "C"])
        tracker.add_stage("UNIVERSE_FILTER", ["A", "B", "C"], ["A", "B"])
        tracker.add_stage("MARKET_DATA", ["A", "B"], ["A", "B"])
        tracker.add_stage("SCORING_ELIGIBLE", ["A", "B"], ["A", "B"])
        tracker.add_stage("BASE_RANKING", ["A", "B"], ["A", "B"])
        tracker.add_stage("FORMAL_ELIGIBILITY", ["A", "B"], ["A", "B"])
        tracker.add_stage("COMPOSITION_FILTER", ["A", "B"], ["A", "B"])
        tracker.add_stage("DATA_QUALITY", ["A", "B"], [])  # All rejected
        tracker.add_stage("FORMAL_TOP", ["A", "B"], ["A", "B"])  # Fallback from preliminary

        tracker.mark_quality_fallback(
            preview_symbols=["A", "B"],
            formal_symbols=[],
        )

        # DATA_QUALITY itself satisfies output ≤ input
        dq = tracker.records[-2]
        assert dq.output_count == 0
        assert dq.output_count <= dq.input_count

        # Fallback is recognized and chain break is suppressed
        validation = tracker.validate()
        assert validation["consistent"] is True
        assert validation["quality_fallback_active"] is True

    # ── Zero-case edges ────────────────────────────────────────────────────

    def test_zero_output_is_valid(self, tmp_path: Path):
        """Zero output (all candidates filtered) satisfies 0 ≤ input."""
        tracker = FunnelTracker(selection_run_id="zero-out", selection_date="2026-07-24",
                                project_dir=tmp_path)
        tracker.add_stage("UNIVERSE", ["A", "B"], ["A", "B"])
        tracker.add_stage("DATA_QUALITY", ["A", "B"], [])  # 2→0, valid

        assert tracker.records[-1].output_count == 0
        assert tracker.records[-1].output_count <= tracker.records[-1].input_count
        assert tracker.records[-1].status == "PASS"

    def test_zero_to_zero_is_valid(self, tmp_path: Path):
        """0→0 is valid (nothing to pass through)."""
        tracker = FunnelTracker(selection_run_id="zero-zero", selection_date="2026-07-24",
                                project_dir=tmp_path)
        tracker.add_stage("UNIVERSE", ["A"], ["A"])
        tracker.add_stage("UNIVERSE_FILTER", ["A"], [])  # 1→0
        tracker.add_stage("MARKET_DATA", [], [])  # 0→0

        assert tracker.records[-1].output_count == 0
        assert tracker.records[-1].input_count == 0
        assert tracker.records[-1].status == "PASS"
