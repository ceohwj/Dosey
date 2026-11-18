"""Execute the non-authoritative synthetic Candidate matrix with the real pure resolver."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.services.candidate_search_resolution import _ConservativeAttributeMatcher, _LexicalRelevanceEvaluator
from app.services.rag.candidate_policy import CandidateStage, ResolverPolicy, policy_is_valid
from app.services.rag.candidate_resolver import (
    CandidateHit,
    CandidateIndexMode,
    CandidateProvenanceReceipt,
    CandidateSourceRef,
    HydratedCandidateEvidence,
    IngredientHit,
    MedicationResolver,
    OfficialEntityType,
    OfficialIdentity,
    ProductSnapshot,
    ProductStatus,
    ResolverFailure,
    ResolverInput,
    ResolverResult,
)

_FIXTURE_PATH = Path(__file__).resolve().parents[3] / "evals/candidate/synthetic-search-v1.json"
_MATRIX: dict[str, Any] = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))


class _FixtureIndexPort:
    def __init__(self, evidence: HydratedCandidateEvidence) -> None:
        self.evidence = evidence
        self.reads = 0

    def hydrate(self, request: object) -> HydratedCandidateEvidence:
        self.reads += 1
        return self.evidence


def _policy() -> ResolverPolicy:
    data = _MATRIX["fixture_policy"]
    policy = ResolverPolicy(
        policy_version=data["policy_version"],
        maximum_input_length=data["maximum_input_length"],
        retrieval_limit=data["retrieval_limit"],
        enable_dense=data["enable_dense"],
        release_eligible=data["release_eligible"],
        stage_weights=tuple((CandidateStage(stage), weight) for stage, weight in data["stage_weights"].items()),
        rrf_k=data["rrf_k"],
        minimum_relevance=data["minimum_relevance"],
        minimum_margin=data["minimum_margin"],
        auto_select_stages=frozenset(CandidateStage(stage) for stage in data["auto_select_stages"]),
    )
    assert policy_is_valid(policy)
    assert not policy.enable_dense and not policy.release_eligible
    return policy


def _evidence(case: dict[str, Any]) -> HydratedCandidateEvidence:
    provenance = _MATRIX["provenance"]
    source_id = provenance["source_snapshot_id"]
    index_version = provenance["index_version"]
    receipt = CandidateProvenanceReceipt(
        index_version=case.get("provenance_index_version", index_version),
        catalog_version=provenance["catalog_version"],
        catalog_manifest_hash=provenance["catalog_manifest_hash"],
        source_refs=(CandidateSourceRef(source_id, provenance["source_version"]),),
        normalization_version=provenance["normalization_version"],
        embedding_model_version=None,
        index_mode=CandidateIndexMode(provenance["index_mode"]),
    )
    product_hits: list[CandidateHit] = []
    next_stage_rank: dict[CandidateStage, int] = {}
    for row in case.get("product_hits", []):
        stage = CandidateStage(row["stage"])
        next_stage_rank[stage] = next_stage_rank.get(stage, 0) + 1
        identity = OfficialIdentity(OfficialEntityType.PRODUCT, "MFDS_ITEM_SEQ", row["code"])
        product = ProductSnapshot(
            identity=identity,
            product_name=row["name"],
            strength_text=row["strength_text"],
            dosage_form="합성 정제",
            manufacturer_name="합성 제조사",
            status=ProductStatus(row.get("status", "ACTIVE")),
        )
        product_hits.append(
            CandidateHit(
                identity=identity,
                product=product,
                stage=stage,
                rank=row.get("rank", next_stage_rank[stage]),
                stage_score=row["score"],
                index_version=index_version,
                member_key="b" * 64,
                catalog_version=provenance["catalog_version"],
                source_snapshot_id=row.get("source_snapshot_id", source_id),
                normalization_version=provenance["normalization_version"],
                embedding_model_version=None,
            )
        )
    ingredient_hits = tuple(
        IngredientHit(
            identity=OfficialIdentity(OfficialEntityType.INGREDIENT, "MFDS_INGREDIENT_CODE", row["code"]),
            rank=number,
            stage_score=row["score"],
            index_version=index_version,
            catalog_version=provenance["catalog_version"],
            source_snapshot_id=source_id,
            normalization_version=provenance["normalization_version"],
        )
        for number, row in enumerate(case.get("ingredient_hits", []), start=1)
    )
    return HydratedCandidateEvidence(receipt, tuple(product_hits), ingredient_hits)


def test_fixture_is_explicit_non_release_synthetic_dev_evidence() -> None:
    assert _MATRIX["schema"] == "candidate-synthetic-search-eval-v1"
    assert _MATRIX["status"] == "NON_AUTHORITATIVE_SYNTHETIC_DEV"
    assert _MATRIX["release_eligible"] is False
    assert "no clinical quality" in _MATRIX["purpose"]
    assert _MATRIX["fixture_policy"]["policy_version"].startswith("synthetic-")
    assert len(_MATRIX["cases"]) == 16
    ids = [case["id"] for case in _MATRIX["cases"]]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("case", _MATRIX["cases"], ids=lambda case: case["id"])
def test_synthetic_candidate_matrix(case: dict[str, Any]) -> None:
    policy = _policy()
    index_port = _FixtureIndexPort(_evidence(case))
    resolver = MedicationResolver(
        index_port=index_port,
        attribute_matcher=_ConservativeAttributeMatcher(),
        relevance_evaluator=_LexicalRelevanceEvaluator(),
    )
    data = case["input"]
    actual = resolver.resolve(
        ResolverInput(
            medication_name=data["medication_name"],
            strength_text=data["strength_text"],
            index_version=_MATRIX["provenance"]["index_version"],
            policy_version=policy.policy_version,
        ),
        policy,
    )
    expected = case["expected"]
    if "failure" in expected:
        assert isinstance(actual, ResolverFailure)
        assert actual.reason.value == expected["failure"]
        return
    assert isinstance(actual, ResolverResult)
    assert actual.outcome.value == expected["outcome"]
    assert (actual.candidate.identity.canonical_code if actual.candidate else None) == expected["selected_code"]
    assert actual.eligible_count == expected["eligible_count"]
    if "ingredient_hit_count" in expected:
        assert actual.ingredient_hit_count == expected["ingredient_hit_count"]
    assert index_port.reads == expected.get("index_reads", 1)
