from __future__ import annotations

import unicodedata
from decimal import Decimal
from fractions import Fraction
from typing import Any, cast

import pytest

from ai_worker.tasks.rag.evidence_rank_fusion import (
    CandidateStageSignal,
    FractionReceipt,
    HybridFusionCandidate,
    HybridFusionResult,
    StableCoordinate,
    fuse_hybrid_rrf,
)
from ai_worker.tasks.rag.evidence_reranker import (
    DEFAULT_MAX_INPUT_CANDIDATES,
    DEFAULT_STUB_MARKERS,
    RERANKER_ALGORITHM_ID,
    EvidenceRerankerError,
    EvidenceRerankerErrorCode,
    RerankCandidateInput,
    RerankerConfig,
    rerank_evidence_candidates,
    rerank_hybrid_fusion_results,
)


def _coord(
    source_code: str = "MFDS",
    source_version: str = "v1",
    external_document_id: str = "doc-1",
    chunk_index: int = 0,
) -> StableCoordinate:
    return StableCoordinate(
        source_code=source_code,
        source_version=source_version,
        external_document_id=external_document_id,
        chunk_index=chunk_index,
    )


def _cand(
    doc_id: str,
    text: str,
    *,
    stage1_rank: int = 1,
    content_hash: str | None = None,
    stage1_score: Fraction | Decimal | None = None,
    product_code: str | None = None,
    topic: str | None = None,
    chunk_index: int = 0,
    source_code: str = "MFDS",
) -> RerankCandidateInput:
    return RerankCandidateInput(
        coordinate=_coord(source_code=source_code, external_document_id=doc_id, chunk_index=chunk_index),
        content_hash=content_hash if content_hash is not None else f"hash-{doc_id}-{chunk_index}",
        content_text=text,
        stage1_rank=stage1_rank,
        stage1_score=stage1_score,
        product_code=product_code,
        topic=topic,
    )


def _fusion_result(
    doc_id: str,
    *,
    fusion_rank: int = 1,
    eligible: bool = True,
    content_hash: str | None = None,
) -> HybridFusionResult:
    score = Fraction(1, 60 + max(fusion_rank, 1))
    return HybridFusionResult(
        coordinate=_coord(external_document_id=doc_id),
        content_hash=content_hash if content_hash is not None else f"h-{doc_id}",
        score=score,
        fraction_receipt=FractionReceipt(numerator=str(score.numerator), denominator=str(score.denominator)),
        fusion_rank=fusion_rank,
        is_eligible_for_future_reranker=eligible,
        lexical_signal=CandidateStageSignal(rank=max(fusion_rank, 1), score_decimal="1"),
        dense_signal=None,
    )


@pytest.mark.parametrize("bad_query", ["", "   ", "\t\n  ", "약\x00보관", "\x07경고", "실온\x1f보관"])
def test_empty_or_control_char_query_rejected(bad_query: str) -> None:
    with pytest.raises(EvidenceRerankerError) as exc_info:
        rerank_evidence_candidates(bad_query, [])
    assert exc_info.value.code == EvidenceRerankerErrorCode.EMPTY_QUERY


@pytest.mark.parametrize(
    "bad_config",
    [
        RerankerConfig(algorithm_id="unknown-reranker@1"),
        RerankerConfig(max_input_candidates=0),
        RerankerConfig(max_input_candidates=-5),
        RerankerConfig(max_input_candidates=True),
        RerankerConfig(top_k=0),
        RerankerConfig(top_k=-1),
        RerankerConfig(top_k=False),
        RerankerConfig(stage1_weight=Decimal("-0.01")),
        RerankerConfig(stage1_weight=Decimal("1e25")),
        RerankerConfig(stage1_weight=Decimal("1000.000001")),
        RerankerConfig(entity_weight=Decimal("NaN")),
        RerankerConfig(token_weight=Decimal("Infinity")),
        RerankerConfig(concept_weight=Decimal("-Infinity")),
        RerankerConfig(char_ngram_weight=Decimal("-1")),
        RerankerConfig(stub_penalty=Decimal("-0.1")),
        RerankerConfig(stub_penalty=Decimal("1e25")),
        RerankerConfig(stub_markers=cast(Any, ["빈 내용"])),
        RerankerConfig(stub_markers=("   ",)),
        RerankerConfig(extra_stopwords=cast(Any, "불용어")),
        RerankerConfig(extra_stopwords=("",)),
    ],
)
def test_invalid_config_rejected(bad_config: RerankerConfig) -> None:
    with pytest.raises(EvidenceRerankerError) as exc_info:
        rerank_evidence_candidates("보관 온도 알려주세요", [], config=bad_config)
    assert exc_info.value.code == EvidenceRerankerErrorCode.INVALID_CONFIG


@pytest.mark.parametrize("bad_rank", [0, -1, True, False])
def test_invalid_stage1_rank_rejected(bad_rank: int) -> None:
    cand = _cand("doc-1", "실온에 보관하세요.", stage1_rank=bad_rank)
    with pytest.raises(EvidenceRerankerError) as exc_info:
        rerank_evidence_candidates("보관 방법", [cand])
    assert exc_info.value.code == EvidenceRerankerErrorCode.INVALID_CANDIDATE


@pytest.mark.parametrize(
    "bad_score",
    [
        True,
        False,
        0.75,
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
    ],
)
def test_invalid_stage1_score_rejected(bad_score: object) -> None:
    cand = _cand("doc-1", "내용", stage1_score=cast(Any, bad_score))
    with pytest.raises(EvidenceRerankerError) as exc_info:
        rerank_evidence_candidates("보관 방법", [cand])
    assert exc_info.value.code == EvidenceRerankerErrorCode.INVALID_CANDIDATE


def test_valid_stage1_score_fraction_and_decimal_accepted() -> None:
    c_frac = _cand("doc-1", "실온 보관", stage1_rank=1, stage1_score=Fraction(1, 61))
    c_dec = _cand("doc-2", "실온 보관", stage1_rank=2, stage1_score=Decimal("0.016393"))
    outcome = rerank_evidence_candidates("실온 보관", [c_frac, c_dec])
    assert len(outcome.results) == 2


def test_invalid_candidate_metadata_types_rejected() -> None:
    cand_blank_hash = _cand("doc-1", "내용", content_hash="   ")
    with pytest.raises(EvidenceRerankerError) as exc_hash:
        rerank_evidence_candidates("보관 방법", [cand_blank_hash])
    assert exc_hash.value.code == EvidenceRerankerErrorCode.INVALID_CANDIDATE

    cand_bad_coord = RerankCandidateInput(
        coordinate=cast(Any, "not-a-coord"),
        content_hash="h-1",
        content_text="실온 보관",
        stage1_rank=1,
    )
    with pytest.raises(EvidenceRerankerError) as exc_coord:
        rerank_evidence_candidates("보관 방법", [cand_bad_coord])
    assert exc_coord.value.code == EvidenceRerankerErrorCode.INVALID_CANDIDATE

    cand_bad_product = _cand("doc-2", "실온 보관", product_code=cast(Any, 123))
    with pytest.raises(EvidenceRerankerError) as exc_prod:
        rerank_evidence_candidates("보관 방법", [cand_bad_product])
    assert exc_prod.value.code == EvidenceRerankerErrorCode.INVALID_CANDIDATE

    cand_bad_topic = _cand("doc-3", "실온 보관", topic=cast(Any, ["storage"]))
    with pytest.raises(EvidenceRerankerError) as exc_topic:
        rerank_evidence_candidates("보관 방법", [cand_bad_topic])
    assert exc_topic.value.code == EvidenceRerankerErrorCode.INVALID_CANDIDATE


def test_duplicate_coordinate_and_hash_mismatch() -> None:
    c1 = _cand("doc-1", "실온 보관", stage1_rank=1, content_hash="same-hash")
    c2_same = _cand("doc-1", "실온 보관", stage1_rank=2, content_hash="same-hash")
    with pytest.raises(EvidenceRerankerError) as exc_dup:
        rerank_evidence_candidates("보관 방법", [c1, c2_same])
    assert exc_dup.value.code == EvidenceRerankerErrorCode.DUPLICATE_COORDINATE

    c2_diff = _cand("doc-1", "다른 내용", stage1_rank=2, content_hash="different-hash")
    with pytest.raises(EvidenceRerankerError) as exc_mismatch:
        rerank_evidence_candidates("보관 방법", [c1, c2_diff])
    assert exc_mismatch.value.code == EvidenceRerankerErrorCode.CONTENT_HASH_MISMATCH


def test_empty_candidates_returns_clean_outcome() -> None:
    outcome = rerank_evidence_candidates("복용 전 주의사항 알려주세요", [])
    assert outcome.algorithm_id == RERANKER_ALGORITHM_ID
    assert len(outcome.config_hash) == 64
    assert outcome.input_candidate_count == 0
    assert outcome.evaluated_candidate_count == 0
    assert outcome.results == ()


def test_config_hash_is_deterministic_canonical_and_sensitive_to_opt_in_tables() -> None:
    cfg_a = RerankerConfig(stage1_weight=Decimal("0.20"), top_k=5)
    cfg_b = RerankerConfig(stage1_weight=Decimal("0.2"), top_k=5)
    cfg_weight = RerankerConfig(stage1_weight=Decimal("0.25"), top_k=5)
    cfg_stubs = RerankerConfig(stub_markers=(*DEFAULT_STUB_MARKERS, "추가 빈 항목"))
    cfg_stops = RerankerConfig(extra_stopwords=("임시어",))

    out_a = rerank_evidence_candidates("보관 온도", [], config=cfg_a)
    out_b = rerank_evidence_candidates("보관 온도", [], config=cfg_b)
    out_weight = rerank_evidence_candidates("보관 온도", [], config=cfg_weight)
    out_stubs = rerank_evidence_candidates("보관 온도", [], config=cfg_stubs)
    out_stops = rerank_evidence_candidates("보관 온도", [], config=cfg_stops)

    assert out_a.config_hash == out_b.config_hash
    assert out_a.config_hash != out_weight.config_hash
    assert out_a.config_hash != out_stubs.config_hash
    assert out_a.config_hash != out_stops.config_hash


def test_determinism_under_input_shuffling_and_tie_breaking() -> None:
    c_beta = _cand("beta", "실온에 빛을 피해 보관합니다.", stage1_rank=1)
    c_alpha = _cand("alpha", "실온에 빛을 피해 보관합니다.", stage1_rank=1)
    c_kr = _cand("가나다", "실온에 빛을 피해 보관합니다.", stage1_rank=1)

    out1 = rerank_evidence_candidates("보관 방법", [c_kr, c_beta, c_alpha])
    out2 = rerank_evidence_candidates("보관 방법", [c_alpha, c_kr, c_beta])

    assert [r.coordinate.external_document_id for r in out1.results] == ["alpha", "beta", "가나다"]
    assert out1 == out2
    assert out1.results[0].score == out1.results[1].score == out1.results[2].score
    assert [r.rerank_rank for r in out1.results] == [1, 2, 3]


def test_stage1_prior_monotonically_decreasing_and_exact() -> None:
    c1 = _cand("d1", "무관한 문장", stage1_rank=1)
    c2 = _cand("d2", "무관한 문장", stage1_rank=2)
    c4 = _cand("d4", "무관한 문장", stage1_rank=4)

    outcome = rerank_evidence_candidates("특정질의", [c4, c2, c1])
    priors = {r.coordinate.external_document_id: r.features.stage1_prior for r in outcome.results}
    assert priors["d1"] == Decimal("1.000000")
    assert priors["d2"] == Decimal("0.500000")
    assert priors["d4"] == Decimal("0.250000")


def test_entity_alignment_matches_product_code_and_distinguishes_foreign_codes() -> None:
    query = "SYN-MED-01의 보관 온도를 알려주세요"
    c_match_text = _cand("match-text", "syn-med-01은 실온에서 보관합니다.", stage1_rank=3)
    c_match_field = _cand("match-field", "실온에서 보관합니다.", stage1_rank=3, product_code="SYN-MED-01")
    c_no_code = _cand("no-code", "실온에서 보관합니다.", stage1_rank=2)
    c_wrong_code = _cand("wrong-code", "SYN-MED-02는 실온에서 보관합니다.", stage1_rank=1)

    outcome = rerank_evidence_candidates(query, [c_wrong_code, c_no_code, c_match_text, c_match_field])
    by_doc = {r.coordinate.external_document_id: r for r in outcome.results}

    assert by_doc["match-text"].features.entity_alignment == Decimal("1.000000")
    assert by_doc["match-field"].features.entity_alignment == Decimal("1.000000")
    assert by_doc["no-code"].features.entity_alignment == Decimal("0.250000")
    assert by_doc["wrong-code"].features.entity_alignment == Decimal("0.000000")
    assert outcome.results[0].coordinate.external_document_id in {"match-field", "match-text"}


def test_multi_entity_partial_alignment() -> None:
    query = "MED-01과 MED-02 병용 주의사항"
    c_both = _cand("both", "MED-01과 MED-02 복용 시 주의하세요.", stage1_rank=3)
    c_one = _cand("one", "MED-01 복용 시 주의하세요.", stage1_rank=2)
    c_foreign = _cand("foreign", "MED-99 복용 시 주의하세요.", stage1_rank=1)

    outcome = rerank_evidence_candidates(query, [c_foreign, c_one, c_both])
    by_doc = {r.coordinate.external_document_id: r for r in outcome.results}

    assert by_doc["both"].features.entity_alignment == Decimal("1.000000")
    assert by_doc["one"].features.entity_alignment == Decimal("0.500000")
    assert by_doc["foreign"].features.entity_alignment == Decimal("0.000000")


def test_entity_alignment_zero_when_query_has_no_identifiers() -> None:
    cand = _cand("doc-1", "SYN-001은 실온에 보관합니다.", stage1_rank=1)
    outcome = rerank_evidence_candidates("실온 보관 방법 알려주세요", [cand])
    assert outcome.results[0].features.entity_alignment == Decimal("0.000000")


def test_korean_particle_stripping_stopwords_and_nfc_normalization() -> None:
    query_nfc = "성분은 무엇 인가요 부작용이 있나요 알려주세요"
    query_nfd = unicodedata.normalize("NFD", query_nfc)

    cand = _cand("doc-1", "이 약의 성분과 부작용에 대한 설명입니다.", stage1_rank=1)
    out_nfc = rerank_evidence_candidates(query_nfc, [cand])
    out_nfd = rerank_evidence_candidates(query_nfd, [cand])

    assert out_nfc == out_nfd
    assert out_nfc.results[0].features.token_overlap == Decimal("1.000000")


def test_extra_stopwords_are_opt_in() -> None:
    cand = _cand("doc-1", "이 약의 성분 안내입니다.", stage1_rank=1)
    out_default = rerank_evidence_candidates("평가용 성분 알려주세요", [cand])
    out_opt_in = rerank_evidence_candidates(
        "평가용 성분 알려주세요",
        [cand],
        config=RerankerConfig(extra_stopwords=("평가용",)),
    )
    assert out_default.results[0].features.token_overlap == Decimal("0.500000")
    assert out_opt_in.results[0].features.token_overlap == Decimal("1.000000")


def test_two_char_nouns_ending_in_particle_chars_are_not_mangled() -> None:
    cand = _cand("doc-1", "주의 사항과 온도 조건 및 효과를 확인하세요.", stage1_rank=1)
    outcome = rerank_evidence_candidates("주의 온도 효과", [cand])
    assert outcome.results[0].features.token_overlap == Decimal("1.000000")


def test_concept_overlap_matches_korean_medication_synonyms() -> None:
    query = "먹기 전에 조심할 두드러기 반응이 있으면 의사에게 물어봐야 하나요?"
    c_synonym = _cand(
        "synonym",
        "복용 전 주의할 점으로 알레르기 과민 반응 시 전문가 약사 상담을 권장합니다.",
        stage1_rank=4,
    )
    c_irrelevant = _cand(
        "irrelevant",
        "원래 용기의 뚜껑을 닫아 건조한 보관함에 두어야 합니다.",
        stage1_rank=1,
    )

    outcome = rerank_evidence_candidates(query, [c_irrelevant, c_synonym])
    assert outcome.results[0].coordinate.external_document_id == "synonym"
    assert outcome.results[0].features.concept_overlap == Decimal("1.000000")
    assert outcome.results[1].features.concept_overlap == Decimal("0.000000")


def test_single_char_concept_does_not_false_positive_on_compound_word() -> None:
    query = "물을 충분히 마셔야 하나요?"
    c_hydration = _cand("hydration", "복용 시 수분 섭취를 충분히 하세요.", stage1_rank=2)
    c_drug_word = _cand("drug-word", "약물 성분과 제형 안내입니다.", stage1_rank=1)

    outcome = rerank_evidence_candidates(query, [c_drug_word, c_hydration])
    by_doc = {r.coordinate.external_document_id: r for r in outcome.results}
    assert by_doc["hydration"].features.concept_overlap == Decimal("1.000000")
    assert by_doc["drug-word"].features.concept_overlap == Decimal("0.000000")


def test_char_ngram_overlap_handles_korean_fragments_and_typos() -> None:
    query = "복용누락시 대처법"
    c_near = _cand("near", "복용누락이 발생했을 때의 대처 방법", stage1_rank=2)
    c_far = _cand("far", "식사와 운동 습관 관리", stage1_rank=1)

    outcome = rerank_evidence_candidates(query, [c_far, c_near])
    by_doc = {r.coordinate.external_document_id: r for r in outcome.results}
    assert by_doc["near"].features.char_ngram_overlap > by_doc["far"].features.char_ngram_overlap


def test_stub_penalty_default_generic_and_opt_in_custom_markers() -> None:
    c_generic_stub = _cand(
        "generic-stub",
        "SYN-100은 구체적인 내용은 적혀 있지 않습니다.",
        stage1_rank=1,
    )
    c_corpus_stub = _cand(
        "corpus-stub",
        "SYN-100은 포장 관리 코드만 등록된 항목입니다.",
        stage1_rank=2,
    )
    c_real = _cand(
        "real",
        "SYN-100은 실온에서 습기를 피해 원래 용기에 보관하세요.",
        stage1_rank=3,
    )

    out_default = rerank_evidence_candidates("SYN-100 보관 방법 알려주세요", [c_generic_stub, c_corpus_stub, c_real])
    by_default = {r.coordinate.external_document_id: r for r in out_default.results}
    assert by_default["generic-stub"].features.substantive_adjustment == Decimal("-0.250000")
    assert by_default["corpus-stub"].features.substantive_adjustment == Decimal("0.000000")
    assert by_default["real"].features.substantive_adjustment == Decimal("0.000000")
    assert out_default.results[0].coordinate.external_document_id == "real"

    cfg_opt_in = RerankerConfig(stub_markers=(*DEFAULT_STUB_MARKERS, "포장 관리 코드만 등록된"))
    out_opt_in = rerank_evidence_candidates(
        "SYN-100 보관 방법 알려주세요",
        [c_generic_stub, c_corpus_stub, c_real],
        config=cfg_opt_in,
    )
    by_opt_in = {r.coordinate.external_document_id: r for r in out_opt_in.results}
    assert by_opt_in["corpus-stub"].features.substantive_adjustment == Decimal("-0.250000")

    out_meta = rerank_evidence_candidates("SYN-100 관리 코드와 색인 정보 알려주세요", [c_generic_stub, c_real])
    by_meta = {r.coordinate.external_document_id: r for r in out_meta.results}
    assert by_meta["generic-stub"].features.substantive_adjustment == Decimal("0.000000")


def test_exact_score_formula_matches_feature_breakdown() -> None:
    cfg = RerankerConfig()
    cand = _cand("doc-1", "SYN-001의 복용 전 주의사항과 알레르기 안내입니다.", stage1_rank=2)
    outcome = rerank_evidence_candidates("SYN-001 복용 전 알레르기 주의", [cand], config=cfg)
    result = outcome.results[0]
    f = result.features
    expected = (
        cfg.stage1_weight * f.stage1_prior
        + cfg.entity_weight * f.entity_alignment
        + cfg.token_weight * f.token_overlap
        + cfg.concept_weight * f.concept_overlap
        + cfg.char_ngram_weight * f.char_ngram_overlap
        + f.substantive_adjustment
    ).quantize(Decimal("0.000001"))
    assert result.score == expected


def test_max_input_candidates_and_top_k_capping_and_top_k_exceeding_candidates() -> None:
    candidates = [
        _cand(f"doc-{i:02d}", "실온 보관 안내", stage1_rank=i) for i in range(1, DEFAULT_MAX_INPUT_CANDIDATES + 6)
    ]
    cfg = RerankerConfig(max_input_candidates=10, top_k=3)
    outcome = rerank_evidence_candidates("실온 보관", candidates, config=cfg)

    assert outcome.input_candidate_count == DEFAULT_MAX_INPUT_CANDIDATES + 5
    assert outcome.evaluated_candidate_count == 10
    assert len(outcome.results) == 3
    assert all(r.stage1_rank <= 10 for r in outcome.results)

    small_outcome = rerank_evidence_candidates("실온 보관", candidates[:2], config=RerankerConfig(top_k=10))
    assert small_outcome.input_candidate_count == 2
    assert small_outcome.evaluated_candidate_count == 2
    assert len(small_outcome.results) == 2


def test_rerank_hybrid_fusion_results_filters_eligible_and_hydrates_content() -> None:
    hybrid_inputs = [
        HybridFusionCandidate(
            coordinate=_coord(external_document_id=f"doc-{i:02d}"),
            content_hash=f"h-{i:02d}",
            lexical_signal=CandidateStageSignal(rank=i, score_decimal="1"),
            dense_signal=CandidateStageSignal(rank=i, score_decimal="0.9"),
        )
        for i in range(1, 8)
    ]
    fused = fuse_hybrid_rrf(hybrid_inputs, rrf_k=60, hybrid_limit=7, reranker_input_limit=4)

    content_map: dict[StableCoordinate, str] = {
        fused[0].coordinate: "구체적인 내용은 적혀 있지 않습니다.",
        fused[1].coordinate: "식사와 수분 섭취에 관한 일반 안내입니다.",
        fused[2].coordinate: "보관 온도는 실온이며 빛과 습기를 차단해야 합니다.",
        fused[3].coordinate: "원래 용기의 뚜껑을 잘 닫아 두세요.",
    }

    outcome = rerank_hybrid_fusion_results(
        "보관 온도와 빛 차단 방법",
        fused,
        content_map,
        config=RerankerConfig(max_input_candidates=4, top_k=3),
    )
    assert outcome.input_candidate_count == 4
    assert outcome.evaluated_candidate_count == 4
    assert len(outcome.results) == 3
    assert outcome.results[0].coordinate == fused[2].coordinate
    assert outcome.results[0].stage1_rank == 3
    assert outcome.results[0].rerank_rank == 1


def test_rerank_hybrid_fusion_results_preserves_pre_cap_count_and_validates_beyond_cap() -> None:
    eligible_items = [_fusion_result(f"doc-{i:02d}", fusion_rank=i, eligible=True) for i in range(1, 7)]
    content_map = {item.coordinate: "실온 보관 안내" for item in eligible_items[:3]}

    outcome = rerank_hybrid_fusion_results(
        "실온 보관",
        eligible_items,
        content_map,
        config=RerankerConfig(max_input_candidates=3, top_k=2),
    )
    assert outcome.input_candidate_count == 6
    assert outcome.evaluated_candidate_count == 3
    assert len(outcome.results) == 2

    # Duplicate coordinate beyond cap must still raise DUPLICATE_COORDINATE
    dup_beyond_cap = [
        *eligible_items[:4],
        _fusion_result("doc-01", fusion_rank=5, eligible=True, content_hash="h-doc-01"),
    ]
    with pytest.raises(EvidenceRerankerError) as exc_dup:
        rerank_hybrid_fusion_results(
            "실온 보관",
            dup_beyond_cap,
            content_map,
            config=RerankerConfig(max_input_candidates=2),
        )
    assert exc_dup.value.code == EvidenceRerankerErrorCode.DUPLICATE_COORDINATE

    # Hash mismatch beyond cap must still raise CONTENT_HASH_MISMATCH
    mismatch_beyond_cap = [
        *eligible_items[:4],
        _fusion_result("doc-01", fusion_rank=5, eligible=True, content_hash="other-hash"),
    ]
    with pytest.raises(EvidenceRerankerError) as exc_mismatch:
        rerank_hybrid_fusion_results(
            "실온 보관",
            mismatch_beyond_cap,
            content_map,
            config=RerankerConfig(max_input_candidates=2),
        )
    assert exc_mismatch.value.code == EvidenceRerankerErrorCode.CONTENT_HASH_MISMATCH

    # Invalid fusion_rank beyond cap must still raise INVALID_CANDIDATE
    bad_rank_beyond_cap = [*eligible_items[:3], _fusion_result("doc-99", fusion_rank=0, eligible=True)]
    with pytest.raises(EvidenceRerankerError) as exc_rank:
        rerank_hybrid_fusion_results(
            "실온 보관",
            bad_rank_beyond_cap,
            content_map,
            config=RerankerConfig(max_input_candidates=2),
        )
    assert exc_rank.value.code == EvidenceRerankerErrorCode.INVALID_CANDIDATE


def test_rerank_hybrid_fusion_results_empty_ineligible_and_product_code_map() -> None:
    empty_out = rerank_hybrid_fusion_results("실온 보관", [], {})
    assert empty_out.input_candidate_count == 0
    assert empty_out.evaluated_candidate_count == 0
    assert empty_out.results == ()

    ineligible_items = [_fusion_result("doc-1", fusion_rank=1, eligible=False)]
    ineligible_out = rerank_hybrid_fusion_results("실온 보관", ineligible_items, {})
    assert ineligible_out.input_candidate_count == 0
    assert ineligible_out.evaluated_candidate_count == 0
    assert ineligible_out.results == ()

    f1 = _fusion_result("doc-1", fusion_rank=1, eligible=True)
    f2 = _fusion_result("doc-2", fusion_rank=2, eligible=True)
    content_map = {
        f1.coordinate: "실온에 보관합니다.",
        f2.coordinate: "실온에 보관합니다.",
    }
    product_map = {
        f1.coordinate: "MED-99",
        f2.coordinate: "MED-01",
    }
    out = rerank_hybrid_fusion_results(
        "MED-01 실온 보관",
        [f1, f2],
        content_map,
        product_code_by_coordinate=product_map,
    )
    assert out.results[0].coordinate == f2.coordinate
    assert out.results[0].features.entity_alignment == Decimal("1.000000")
    assert out.results[1].features.entity_alignment == Decimal("0.000000")


def test_rerank_hybrid_fusion_results_missing_content_raises() -> None:
    hybrid_inputs = [
        HybridFusionCandidate(
            coordinate=_coord(external_document_id="doc-1"),
            content_hash="h-1",
            lexical_signal=CandidateStageSignal(rank=1, score_decimal="1"),
        ),
        HybridFusionCandidate(
            coordinate=_coord(external_document_id="doc-2"),
            content_hash="h-2",
            lexical_signal=CandidateStageSignal(rank=2, score_decimal="0.8"),
        ),
    ]
    fused = fuse_hybrid_rrf(hybrid_inputs, rrf_k=60, hybrid_limit=5, reranker_input_limit=5)

    with pytest.raises(EvidenceRerankerError) as exc_info:
        rerank_hybrid_fusion_results(
            "보관 방법",
            fused,
            {fused[0].coordinate: "실온 보관"},
        )
    assert exc_info.value.code == EvidenceRerankerErrorCode.MISSING_CONTENT
