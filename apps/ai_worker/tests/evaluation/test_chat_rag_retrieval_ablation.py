"""Offline unit tests for the Chat RAG retrieval ablation runner."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from ai_worker.tasks.evaluation.chat_rag_retrieval_ablation import (
    DEFAULT_EMBEDDING_DIMENSIONS,
    DEFAULT_EMBEDDING_MODEL,
    EMBEDDING_COST_PER_MILLION_TOKENS_USD,
    BM25Index,
    EmbeddingBatchResult,
    ScoredChunk,
    build_eval_queries,
    compute_query_retrieval_metrics,
    load_eval_corpus,
    main,
    rrf_fuse,
    run_retrieval_ablation,
    summarize_latencies_ms,
    tokenize_korean_bm25,
    validate_cache_path_outside_repo,
)
from ai_worker.tasks.evaluation.mfds_eval_corpus import EvalCorpusChunk, discover_repo_root


def _make_chunk(
    *,
    item_seq: str,
    item_name: str,
    section: str,
    index: int,
    content: str,
    category: str = "test",
) -> EvalCorpusChunk:
    return EvalCorpusChunk(
        chunk_id=f"mfds-15095677:{item_seq}:{section}:{index}",
        item_seq=item_seq,
        item_name=item_name,
        section=section,  # type: ignore[arg-type]
        locator=f"{section}:p{index}",
        source_code="MFDS_DATA_GO_KR_15095677_EVAL",
        source_version="mfds-15095677:test:0001",
        content=content,
        content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        category=category,
    )


def _sample_chunks() -> tuple[EvalCorpusChunk, ...]:
    return (
        _make_chunk(
            item_seq="200610660",
            item_name="노바스크정5밀리그램(암로디핀베실산염)",
            section="EE",
            index=0,
            content="적응증: 본태성 고혈압과 관상동맥 심장질환(만성 안정형 협심증) 치료에 사용합니다.",
            category="hypertension",
        ),
        _make_chunk(
            item_seq="200610660",
            item_name="노바스크정5밀리그램(암로디핀베실산염)",
            section="UD",
            index=0,
            content="용법용량: 성인은 1일 1회 5mg을 경구 투여하며 최대 1일 10mg까지 증량할 수 있습니다.",
            category="hypertension",
        ),
        _make_chunk(
            item_seq="200610660",
            item_name="노바스크정5밀리그램(암로디핀베실산염)",
            section="NB",
            index=0,
            content="주의사항: 임부 및 수유부에게는 투여하지 않으며 심한 간장애 환자와 병용투여 시 주의하십시오.",
            category="hypertension",
        ),
        _make_chunk(
            item_seq="199900101",
            item_name="다이아벡스정500밀리그램(메트포르민염산염)",
            section="EE",
            index=0,
            content="효능효과: 제2형 당뇨병 환자의 혈당 조절을 향상시키기 위해 식이요법과 함께 투여합니다.",
            category="diabetes",
        ),
        _make_chunk(
            item_seq="199900101",
            item_name="다이아벡스정500밀리그램(메트포르민염산염)",
            section="UD",
            index=0,
            content="용법용량: 식후 또는 식사 직후 충분한 물과 함께 1일 2회 분할 복용합니다.",
            category="diabetes",
        ),
        _make_chunk(
            item_seq="199900101",
            item_name="다이아벡스정500밀리그램(메트포르민염산염)",
            section="NB",
            index=0,
            content="경고: 음주 및 신장애 환자에서는 유산산증 위험이 높아지므로 복용을 금합니다.",
            category="diabetes",
        ),
        _make_chunk(
            item_seq="200801234",
            item_name="타이레놀정500밀리그람(아세트아미노펜)",
            section="EE",
            index=0,
            content="효능효과: 감기로 인한 발열 및 두통, 치통, 근육통 등 통증의 해열과 진통에 사용합니다.",
            category="analgesic_nsaid",
        ),
        _make_chunk(
            item_seq="200801234",
            item_name="타이레놀정500밀리그람(아세트아미노펜)",
            section="UD",
            index=0,
            content="용법용량: 성인은 1회 1~2정씩 1일 3~4회 필요 시 복용하며 1일 최대 4000mg을 초과하지 마십시오.",
            category="analgesic_nsaid",
        ),
        _make_chunk(
            item_seq="200801234",
            item_name="타이레놀정500밀리그람(아세트아미노펜)",
            section="NB",
            index=0,
            content="경고: 매일 세 잔 이상 정기적으로 술을 마시는 사람은 간독성 위험으로 의사 또는 약사와 상의하십시오.",
            category="analgesic_nsaid",
        ),
    )


def _write_jsonl(path: Path, chunks: Sequence[EvalCorpusChunk]) -> Path:
    lines = [
        json.dumps(
            {
                "chunk_id": c.chunk_id,
                "item_seq": c.item_seq,
                "item_name": c.item_name,
                "section": c.section,
                "locator": c.locator,
                "source_code": c.source_code,
                "source_version": c.source_version,
                "content": c.content,
                "content_sha256": c.content_sha256,
                "category": c.category,
            },
            ensure_ascii=False,
        )
        for c in chunks
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class _DeterministicFakeEmbedder:
    """Offline deterministic embedder mapping drug/section semantics to fixed axes."""

    def __init__(self, *, dims: int = 8, tokens_per_text: int = 100) -> None:
        self.dims = dims
        self.tokens_per_text = tokens_per_text
        self.calls: list[list[str]] = []

    def _encode_one(self, text: str) -> tuple[float, ...]:
        vec = [0.0] * self.dims
        # Drug family axes (0, 1, 2)
        if "노바스크" in text or "암로디핀" in text or "고혈압" in text or "협심증" in text:
            vec[0] += 2.0
        if "다이아벡스" in text or "메트포르민" in text or "당뇨" in text or "혈당" in text:
            vec[1] += 2.0
        if "타이레놀" in text or "아세트아미노펜" in text or "해열" in text or "진통" in text or "두통" in text:
            vec[2] += 2.0
        # Section axes (3, 4, 5)
        if "[EE]" in text or "효능" in text or "효과" in text or "적응증" in text or "증상" in text or "질환" in text:
            vec[3] += 1.5
        if "[UD]" in text or "용법" in text or "복용량" in text or "몇 번" in text or "몇 알" in text or "식후" in text:
            vec[4] += 1.5
        if "[NB]" in text or "주의" in text or "부작용" in text or "임신" in text or "술" in text or "금기" in text:
            vec[5] += 1.5
        # Out-of-corpus negative orthogonal axis (6, 7)
        if all(v == 0.0 for v in vec[:6]):
            vec[6] = 1.0
        else:
            vec[7] = 0.05
        return tuple(vec)

    def embed_texts(
        self,
        texts: Sequence[str],
        *,
        model: str = DEFAULT_EMBEDDING_MODEL,
        dimensions: int = DEFAULT_EMBEDDING_DIMENSIONS,
    ) -> EmbeddingBatchResult:
        self.calls.append(list(texts))
        vectors = tuple(self._encode_one(t) for t in texts)
        return EmbeddingBatchResult(
            vectors=vectors,
            prompt_tokens=len(texts) * self.tokens_per_text,
            model=model,
            dimensions=dimensions,
        )


class _AsyncFakeEmbedder(_DeterministicFakeEmbedder):
    async def embed_texts(  # type: ignore[override]
        self,
        texts: Sequence[str],
        *,
        model: str = DEFAULT_EMBEDDING_MODEL,
        dimensions: int = DEFAULT_EMBEDDING_DIMENSIONS,
    ) -> EmbeddingBatchResult:
        return super().embed_texts(texts, model=model, dimensions=dimensions)


def test_load_eval_corpus_fail_closed_on_missing_empty_and_invalid(tmp_path: Path) -> None:
    missing_path = tmp_path / "does_not_exist.jsonl"
    with pytest.raises(FileNotFoundError):
        load_eval_corpus(missing_path)

    empty_path = tmp_path / "empty.jsonl"
    empty_path.write_text("   \n", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        load_eval_corpus(empty_path)

    bad_section_path = tmp_path / "bad_section.jsonl"
    bad_section_path.write_text(
        json.dumps(
            {
                "chunk_id": "c1",
                "item_seq": "200610660",
                "item_name": "노바스크정",
                "section": "INVALID",
                "content": "내용",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Invalid section"):
        load_eval_corpus(bad_section_path)


def test_security_guards_repo_cache_missing_api_key_and_budget_cap(tmp_path: Path) -> None:
    repo_root = discover_repo_root()
    with pytest.raises(ValueError, match="outside the git repository"):
        validate_cache_path_outside_repo(repo_root / "embeddings_cache.json", repo_root=repo_root)

    corpus_path = _write_jsonl(tmp_path / "corpus_chunks.jsonl", _sample_chunks())
    cache_path = tmp_path / "embeddings_cache.json"

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        run_retrieval_ablation(
            corpus_path=corpus_path,
            cache_path=cache_path,
            embedder=None,
            env={},
            min_queries=15,
        )

    fake_embedder = _DeterministicFakeEmbedder(dims=8)
    with pytest.raises(ValueError, match="ADR 0004"):
        run_retrieval_ablation(
            corpus_path=corpus_path,
            cache_path=cache_path,
            embedder=fake_embedder,
            embedding_dimensions=8,
            min_queries=60,
            max_cost_usd=0.000001,
        )
    assert fake_embedder.calls == []


def test_build_eval_queries_grounded_spanning_sections_colloquial_and_negatives() -> None:
    chunks = _sample_chunks()
    chunk_map = {c.chunk_id: c for c in chunks}

    queries_a = build_eval_queries(chunks, min_queries=60)
    queries_b = build_eval_queries(chunks, min_queries=60)
    assert queries_a == queries_b
    assert len(queries_a) >= 60

    query_types = {q.query_type for q in queries_a}
    assert {"dosage_ud", "efficacy_ee", "warning_nb", "colloquial_natural", "out_of_corpus_negative"} <= query_types

    positives = [q for q in queries_a if not q.is_negative]
    negatives = [q for q in queries_a if q.is_negative]
    assert len(positives) >= 48
    assert len(negatives) >= 8
    assert len({q.query for q in queries_a}) == len(queries_a)

    for q in positives:
        assert q.expected_item_seq is not None
        assert q.expected_section in {"EE", "UD", "NB"}
        assert len(q.relevant_chunk_ids) >= 1
        for cid in q.relevant_chunk_ids:
            assert cid in chunk_map
            assert chunk_map[cid].item_seq == q.expected_item_seq
            assert chunk_map[cid].section == q.expected_section

    for q in negatives:
        assert q.expected_item_seq is None
        assert q.expected_section is None
        assert q.relevant_chunk_ids == ()


def test_bm25_subword_korean_tokenization_and_metric_math() -> None:
    tokens = tokenize_korean_bm25("노바스크정을 복용")
    assert "w:노바스크정을" in tokens
    assert "bg:노바" in tokens
    assert "bg:바스" in tokens
    assert "bg:스크" in tokens

    index = BM25Index(_sample_chunks())
    results = index.search("노바스크정을 고혈압 치료에 먹어도 되나요", top_k=3)
    assert results
    assert results[0].chunk_id == "mfds-15095677:200610660:EE:0"

    scores = compute_query_retrieval_metrics(
        ranked_chunk_ids=["x1", "c1", "x2", "c2", "x3"],
        relevant_chunk_ids=["c1", "c2"],
        k=5,
    )
    assert scores["recall_at_k"] == pytest.approx(1.0)
    assert scores["mrr_at_k"] == pytest.approx(0.5)
    expected_dcg = (1.0 / 1.5849625007211563) + (1.0 / 2.321928094887362)
    expected_idcg = 1.0 + (1.0 / 1.5849625007211563)
    assert scores["ndcg_at_k"] == pytest.approx(expected_dcg / expected_idcg, rel=1e-6)
    assert scores["no_hit"] == 0.0

    p50, p95 = summarize_latencies_ms([1.0, 2.0, 3.0, 4.0, 100.0])
    assert p50 == 3.0
    assert p95 == 100.0


def test_rrf_fuse_deterministic_tie_breaking_and_hybrid_improvement() -> None:
    # Identical RRF score (1/61) and best_rank (1) -> broken strictly by chunk_id ascending.
    fused_a = rrf_fuse(
        [ScoredChunk(chunk_id="chunk-z", score=10.0, rank=1)],
        [ScoredChunk(chunk_id="chunk-a", score=0.9, rank=1)],
        rrf_k=60,
        top_k=5,
    )
    assert [item.chunk_id for item in fused_a] == ["chunk-a", "chunk-z"]
    assert fused_a[0].score == fused_a[1].score

    # Single-arm miss lift: target chunk is rank 4 in BM25 (outside top-1) and rank 3 in Dense,
    # while wrong distractors only appear in one arm at rank 1-2. RRF promotes the shared chunk!
    bm25_pool = [
        ScoredChunk(chunk_id="bm25-only-1", score=9.0, rank=1),
        ScoredChunk(chunk_id="bm25-only-2", score=8.0, rank=2),
        ScoredChunk(chunk_id="bm25-only-3", score=7.0, rank=3),
        ScoredChunk(chunk_id="shared-gold", score=6.5, rank=4),
    ]
    dense_pool = [
        ScoredChunk(chunk_id="dense-only-1", score=0.95, rank=1),
        ScoredChunk(chunk_id="dense-only-2", score=0.90, rank=2),
        ScoredChunk(chunk_id="shared-gold", score=0.88, rank=3),
    ]
    fused_hybrid = rrf_fuse(bm25_pool, dense_pool, rrf_k=60, top_k=5)
    assert fused_hybrid[0].chunk_id == "shared-gold"


def test_embedding_cache_hit_miss_reuse_async_embedder_and_cli_outputs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    corpus_path = _write_jsonl(tmp_path / "corpus_chunks.jsonl", _sample_chunks())
    cache_path = tmp_path / "embeddings_cache.json"
    fake_embedder = _AsyncFakeEmbedder(dims=8, tokens_per_text=100)

    report_first = run_retrieval_ablation(
        corpus_path=corpus_path,
        cache_path=cache_path,
        embedder=fake_embedder,
        embedding_dimensions=8,
        min_queries=60,
    )
    assert report_first.mode == "full_ablation_injected"
    assert report_first.total_queries >= 60
    assert cache_path.exists()
    assert report_first.dense_summary.cache_hits == 0
    misses = report_first.dense_summary.cache_misses
    assert misses > 0
    assert report_first.dense_summary.prompt_tokens == misses * 100
    expected_cost = round((misses * 100 / 1_000_000.0) * EMBEDDING_COST_PER_MILLION_TOKENS_USD, 8)
    assert report_first.dense_summary.estimated_cost_usd == pytest.approx(expected_cost)
    assert len(fake_embedder.calls) >= 1

    # Verify positive/negative separation and metrics across all 3 arms.
    for arm_name in ("bm25_lexical", "dense_vector", "rrf_hybrid"):
        arm = report_first.arms[arm_name]
        assert arm.executed is True
        assert arm.recall_at_5 is not None and arm.recall_at_5 > 0.7
        assert arm.mrr_at_5 is not None and arm.mrr_at_5 > 0.5
        assert arm.ndcg_at_5 is not None and arm.ndcg_at_5 > 0.5
        assert arm.pos_neg_top1_gap is not None and arm.pos_neg_top1_gap > 0.0
        assert arm.latency_p50_ms is not None and arm.latency_p95_ms is not None

    # Second run reuses cache completely without calling embedder again.
    prior_call_count = len(fake_embedder.calls)
    report_cached = run_retrieval_ablation(
        corpus_path=corpus_path,
        cache_path=cache_path,
        embedder=fake_embedder,
        embedding_dimensions=8,
        min_queries=60,
    )
    assert len(fake_embedder.calls) == prior_call_count
    assert report_cached.dense_summary.cache_misses == 0
    assert report_cached.dense_summary.cache_hits == misses
    assert report_cached.dense_summary.api_calls == 0
    assert report_cached.dense_summary.prompt_tokens == 0

    # CLI JSON + Markdown + dry-run + offline-bm25-only
    assert (
        main(
            [
                "--corpus-path",
                str(corpus_path),
                "--cache-path",
                str(cache_path),
                "--format",
                "json",
                "--offline-bm25-only",
            ]
        )
        == 0
    )
    json_payload = json.loads(capsys.readouterr().out)
    assert json_payload["mode"] == "offline_bm25_only"
    assert json_payload["arms"]["bm25_lexical"]["executed"] is True
    assert json_payload["arms"]["dense_vector"]["executed"] is False

    assert (
        main(
            [
                "--corpus-path",
                str(corpus_path),
                "--cache-path",
                str(cache_path),
                "--format",
                "markdown",
                "--dry-run",
            ]
        )
        == 0
    )
    md_out = capsys.readouterr().out
    assert "# Chat RAG Retrieval Ablation Report" in md_out
    assert "| `bm25_lexical` |" in md_out
    assert "| `rrf_hybrid` |" in md_out
