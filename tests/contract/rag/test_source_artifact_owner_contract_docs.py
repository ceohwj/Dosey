from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_source_artifact_owner_decision_and_contract_alignment() -> None:
    decision = (ROOT / "docs/archive/decisions/2026-11-12-source-artifact-owner-protocol-v1.md").read_text(
        encoding="utf-8"
    )
    assert "PD-DOSEY-20261112-SOURCE-ARTIFACT-OWNER-V1-1" in decision
    assert "source-artifact-owner@1" in decision
    assert "source-artifact-intent@1" in decision
    assert "source-artifact-outcome@1" in decision

    boundary_doc = (
        ROOT / "docs/contracts/proposed/post-mvp-1/source-artifact-finalizer-owner-boundary-v1.md"
    ).read_text(encoding="utf-8")
    assert "PD-DOSEY-20261112-SOURCE-ARTIFACT-OWNER-V1-1" in boundary_doc
    assert "source-artifact-owner@1" in boundary_doc
    assert "CLIENT_TRANSPORT_UNKNOWN" in boundary_doc

    target_doc = (ROOT / "docs/contracts/targets/post-mvp-1/rag-source-ingestion-v1.md").read_text(encoding="utf-8")
    assert "2026-11-12-source-artifact-owner-protocol-v1.md" in target_doc
    assert "source-artifact-owner@1" in target_doc

    readme_doc = (ROOT / "docs/contracts/README.md").read_text(encoding="utf-8")
    assert "source-artifact-finalizer-owner-boundary-v1.md" in readme_doc
