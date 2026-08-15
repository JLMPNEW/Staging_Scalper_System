from __future__ import annotations

from pathlib import Path


def test_tanker_coverage_audit_reads_resumed_run_evidence_relation() -> None:
    script = (
        Path(__file__).resolve().parents[2]
        / "industrials"
        / "transportation"
        / "scripts"
        / "36f_audit_transportation_tanker_parser_coverage.py"
    )
    source = script.read_text(encoding="utf-8")

    assert "FROM sec_parser_run_metric_evidence AS relation" in source
    assert "evidence.evidence_key = relation.evidence_key" in source
    assert "WHERE relation.run_id=?" in source
    assert "FROM sec_parser_metric_evidence_shadow\n        WHERE run_id=?" not in source
