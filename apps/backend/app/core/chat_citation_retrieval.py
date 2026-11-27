"""Local opt-in App-DB Chat evidence retriever for claim-citation validation.

Zero imports from `ai_worker` so Backend production code stays strictly within
`tests/contract/test_backend_ai_worker_import_boundary.py`.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from uuid import UUID

from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.knowledge import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeDocumentContractVersion,
    KnowledgeDocumentStatus,
)

DEFAULT_EMBEDDING_MODEL = "text-embedding-3-large"
DEFAULT_EMBEDDING_DIMENSIONS = 1536
CACHE_SCHEMA_VERSION = "chat-rag-embedding-cache@1"
_RRF_K = 60
_POOL_SIZE = 20

_WHITESPACE_RE = re.compile(r"\s+")
_WORD_TOKEN_RE = re.compile(r"[가-힣]{1,}|[0-9]+(?:\.[0-9]+)?|[a-zA-Z]+")

QueryEmbedder = Callable[[str], Awaitable[tuple[float, ...]]]


class ChatCitationRetrievalError(RuntimeError):
    """Fail-closed retrieval error for `AppDbChatEvidenceRetriever`."""


class RevealedTextProtocol(Protocol):
    def reveal(self) -> str: ...


class ChatEvidenceItemProtocol(Protocol):
    @property
    def display_order(self) -> int: ...

    @property
    def knowledge_chunk_id(self) -> UUID: ...

    @property
    def source_code(self) -> str: ...

    @property
    def source_version(self) -> str: ...

    @property
    def locator(self) -> str: ...

    @property
    def content(self) -> RevealedTextProtocol: ...


class ChatEvidenceRetrieverProtocol(Protocol):
    async def retrieve(self, question: str) -> Sequence[ChatEvidenceItemProtocol]: ...


@dataclass(frozen=True, slots=True)
class RevealedEvidenceText:
    """Minimal `.reveal()` wrapper matching `ClosedDemoEvidence.content` without importing `ai_worker`."""

    _text: str = field(repr=False)

    def reveal(self) -> str:
        return self._text


@dataclass(frozen=True, slots=True)
class AppDbRetrievedEvidence:
    """Retrieved app-DB chunk carrying its persisted `knowledge_chunk_id`."""

    display_order: int
    knowledge_chunk_id: UUID
    source_code: str
    source_version: str
    locator: str
    content: RevealedEvidenceText
    content_sha256: str


@dataclass(frozen=True, slots=True)
class _LoadedChunk:
    knowledge_chunk_id: UUID
    source_code: str
    source_version: str
    locator: str
    chunk_text: str
    content_sha256: str
    tokens: tuple[str, ...]
    embedding: tuple[float, ...] | None


def _normalize_text(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", unicodedata.normalize("NFC", text)).strip()


def tokenize_korean_lexical(text: str) -> tuple[str, ...]:
    normalized = _normalize_text(text).lower()
    if not normalized:
        return ()
    tokens: list[str] = []
    for word in _WORD_TOKEN_RE.findall(normalized):
        tokens.append(f"w:{word}")
        if len(word) >= 2:
            for idx in range(len(word) - 1):
                tokens.append(f"bg:{word[idx : idx + 2]}")
    return tuple(tokens)


def _l2_normalize(vector: Sequence[float], *, expected_dim: int) -> tuple[float, ...]:
    if len(vector) != expected_dim:
        raise ChatCitationRetrievalError(f"Expected embedding dimension {expected_dim}, got {len(vector)}")
    floats = [float(val) for val in vector]
    if any(not math.isfinite(val) for val in floats):
        raise ChatCitationRetrievalError("Embedding vector contains non-finite values")
    norm = math.sqrt(sum(val * val for val in floats))
    if norm == 0.0:
        return tuple(0.0 for _ in floats)
    return tuple(val / norm for val in floats)


def _embedding_cache_key(text: str, *, model: str, dimensions: int) -> str:
    return hashlib.sha256(f"{model}:{dimensions}:{text}".encode()).hexdigest()


def load_embedding_cache(
    cache_path: Path | None,
    *,
    model: str,
    dimensions: int,
) -> dict[str, tuple[float, ...]]:
    """Load precomputed local embedding cache if path is provided."""
    if cache_path is None:
        return {}
    if not cache_path.is_file():
        raise ChatCitationRetrievalError(f"Configured embedding cache file does not exist: {cache_path}")
    raw = json.loads(cache_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ChatCitationRetrievalError("Embedding cache JSON root must be an object")
    if raw.get("schema_version") != CACHE_SCHEMA_VERSION:
        raise ChatCitationRetrievalError("Unsupported embedding cache schema_version")
    if raw.get("model") != model or raw.get("dimensions") != dimensions:
        raise ChatCitationRetrievalError("Embedding cache model/dimensions mismatch")
    entries = raw.get("entries")
    if not isinstance(entries, dict):
        raise ChatCitationRetrievalError("Embedding cache entries must be an object")
    return {
        str(key): _l2_normalize(vec, expected_dim=dimensions)
        for key, vec in entries.items()
        if isinstance(vec, Sequence) and not isinstance(vec, (str, bytes))
    }


def _score_bm25(query_tokens: tuple[str, ...], chunks: Sequence[_LoadedChunk]) -> list[tuple[int, float]]:
    if not query_tokens or not chunks:
        return [(idx, 0.0) for idx in range(len(chunks))]
    k1 = 1.5
    b = 0.75
    n_docs = len(chunks)
    lengths = [len(chunk.tokens) for chunk in chunks]
    avg_len = sum(lengths) / n_docs if n_docs else 1.0
    doc_tfs = [Counter(chunk.tokens) for chunk in chunks]
    df: Counter[str] = Counter()
    for tf in doc_tfs:
        for token in tf:
            df[token] += 1

    unique_q = list(dict.fromkeys(query_tokens))
    scores: list[tuple[int, float]] = []
    for idx, tf in enumerate(doc_tfs):
        doc_len = lengths[idx] or 1
        total = 0.0
        for token in unique_q:
            freq = tf.get(token, 0)
            if freq == 0:
                continue
            token_df = df.get(token, 0)
            idf = math.log(1.0 + (n_docs - token_df + 0.5) / (token_df + 0.5))
            denom = freq + k1 * (1.0 - b + b * (doc_len / avg_len))
            total += idf * ((freq * (k1 + 1.0)) / denom)
        scores.append((idx, total))
    scores.sort(key=lambda item: (-item[1], chunks[item[0]].knowledge_chunk_id.hex))
    return scores


def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


class AppDbChatEvidenceRetriever:
    """Retrieves active MFDS/evaluation `knowledge_chunk` rows from app DB.

    When an OpenAI client/query_embedder and precomputed dense vectors are available,
    combines BM25 lexical + dense cosine similarity via deterministic RRF (`k=60`)
    and applies `min_dense_score` filtering for negative-query abstention.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        openai_client: AsyncOpenAI | None = None,
        embedding_cache_path: Path | None = None,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
        embedding_dimension: int = DEFAULT_EMBEDDING_DIMENSIONS,
        top_k: int = 5,
        min_dense_score: float = 0.50,
        query_embedder: QueryEmbedder | None = None,
    ) -> None:
        if top_k <= 0:
            raise ValueError("top_k must be positive")
        if not 0.0 <= min_dense_score <= 1.0:
            raise ValueError("min_dense_score must be in [0, 1]")
        self._session_factory = session_factory
        self._openai_client = openai_client
        self._embedding_model = embedding_model
        self._embedding_dimension = embedding_dimension
        self._top_k = top_k
        self._min_dense_score = min_dense_score
        self._query_embedder = query_embedder
        self._cached_vectors = load_embedding_cache(
            embedding_cache_path,
            model=embedding_model,
            dimensions=embedding_dimension,
        )

    async def _embed_query(self, question: str) -> tuple[float, ...] | None:
        cached = self._cached_vectors.get(
            _embedding_cache_key(question, model=self._embedding_model, dimensions=self._embedding_dimension)
        )
        if cached is not None:
            return cached
        if self._query_embedder is not None:
            raw = await self._query_embedder(question)
            return _l2_normalize(raw, expected_dim=self._embedding_dimension)
        if self._openai_client is None:
            return None
        try:
            response = await self._openai_client.embeddings.create(
                input=[question],
                model=self._embedding_model,
                dimensions=self._embedding_dimension,
                encoding_format="float",
            )
            vector = response.data[0].embedding
        except Exception as exc:
            raise ChatCitationRetrievalError(f"Failed to embed chat query: {type(exc).__name__}") from exc
        return _l2_normalize(vector, expected_dim=self._embedding_dimension)

    async def _load_active_chunks(self) -> list[_LoadedChunk]:
        stmt = (
            select(KnowledgeChunk, KnowledgeDocument)
            .join(KnowledgeDocument, KnowledgeChunk.knowledge_document_id == KnowledgeDocument.id)
            .where(
                KnowledgeDocument.document_status == KnowledgeDocumentStatus.ACTIVE,
                KnowledgeDocument.record_contract_version == KnowledgeDocumentContractVersion.LEGACY_V1,
                KnowledgeChunk.content_hash.is_not(None),
            )
            .order_by(KnowledgeDocument.id, KnowledgeChunk.chunk_index)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).all()

        chunks: list[_LoadedChunk] = []
        for chunk, doc in rows:
            source_code = (doc.publisher or "LOCAL_EVAL_MFDS").strip()
            source_version = (doc.document_version or "local-eval@1").strip()
            section_loc = (
                chunk.normalization_version or chunk.vector_store_key or f"chunk:{chunk.chunk_index}"
            ).strip()
            locator = f"{doc.title} ({section_loc})"[:500]
            cache_key = _embedding_cache_key(
                chunk.chunk_text,
                model=self._embedding_model,
                dimensions=self._embedding_dimension,
            )
            chunks.append(
                _LoadedChunk(
                    knowledge_chunk_id=chunk.id,
                    source_code=source_code[:100],
                    source_version=source_version[:200],
                    locator=locator,
                    chunk_text=chunk.chunk_text,
                    content_sha256=hashlib.sha256(chunk.chunk_text.encode("utf-8")).hexdigest(),
                    tokens=tokenize_korean_lexical(chunk.chunk_text),
                    embedding=self._cached_vectors.get(cache_key),
                )
            )
        return chunks

    async def retrieve(self, question: str) -> tuple[AppDbRetrievedEvidence, ...]:
        normalized_question = _normalize_text(question)
        if not normalized_question:
            raise ChatCitationRetrievalError("Question must not be blank for retrieval")

        chunks = await self._load_active_chunks()
        if not chunks:
            raise ChatCitationRetrievalError("No active indexed knowledge chunks available in app DB")

        query_tokens = tokenize_korean_lexical(normalized_question)
        bm25_ranked = _score_bm25(query_tokens, chunks)

        has_chunk_vectors = any(chunk.embedding is not None for chunk in chunks)
        query_vec = await self._embed_query(normalized_question) if has_chunk_vectors else None

        selected_indices: list[int]
        if query_vec is not None and has_chunk_vectors:
            dense_scores: list[tuple[int, float]] = []
            for idx, chunk in enumerate(chunks):
                score = _cosine(query_vec, chunk.embedding) if chunk.embedding is not None else -1.0
                dense_scores.append((idx, score))
            dense_scores.sort(key=lambda item: (-item[1], chunks[item[0]].knowledge_chunk_id.hex))

            dense_score_by_idx = dict(dense_scores)
            bm25_ranks = {idx: rank for rank, (idx, score) in enumerate(bm25_ranked[:_POOL_SIZE], start=1) if score > 0}
            dense_ranks = {idx: rank for rank, (idx, _) in enumerate(dense_scores[:_POOL_SIZE], start=1)}

            rrf_scores: list[tuple[int, float]] = []
            for idx in set(bm25_ranks) | set(dense_ranks):
                if dense_score_by_idx.get(idx, -1.0) < self._min_dense_score and idx not in bm25_ranks:
                    continue
                if dense_score_by_idx.get(idx, -1.0) < self._min_dense_score * 0.75:
                    continue
                score = (1.0 / (_RRF_K + bm25_ranks[idx]) if idx in bm25_ranks else 0.0) + (
                    1.0 / (_RRF_K + dense_ranks[idx]) if idx in dense_ranks else 0.0
                )
                rrf_scores.append((idx, score))
            rrf_scores.sort(key=lambda item: (-item[1], chunks[item[0]].knowledge_chunk_id.hex))
            selected_indices = [idx for idx, _ in rrf_scores[: self._top_k]]
            if not selected_indices:
                raise ChatCitationRetrievalError("No evidence chunk met retrieval relevance threshold")
        else:
            positive_bm25 = [idx for idx, score in bm25_ranked if score > 0.0]
            if not positive_bm25:
                raise ChatCitationRetrievalError("No lexical evidence match found for chat query")
            selected_indices = positive_bm25[: self._top_k]

        return tuple(
            AppDbRetrievedEvidence(
                display_order=order,
                knowledge_chunk_id=chunks[idx].knowledge_chunk_id,
                source_code=chunks[idx].source_code,
                source_version=chunks[idx].source_version,
                locator=chunks[idx].locator,
                content=RevealedEvidenceText(chunks[idx].chunk_text),
                content_sha256=chunks[idx].content_sha256,
            )
            for order, idx in enumerate(selected_indices, start=1)
        )
