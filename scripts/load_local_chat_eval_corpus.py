"""Idempotent local loader for loading private MFDS evaluation corpus chunks into app DB.

Strictly limited to local PostgreSQL (`DB_HOST` in localhost/127.0.0.1/::1).
Zero imports from `ai_worker`.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import config
from app.core.db.databases import AsyncSessionFactory, close_database
from app.models.knowledge import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeDocumentContractVersion,
    KnowledgeDocumentStatus,
)

DEFAULT_CORPUS_PATH = Path("/tmp/dosey-mfds-eval-corpus/corpus_chunks.jsonl")
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-large"


def _require_local_database() -> None:
    if config.DB_HOST not in ("localhost", "127.0.0.1", "::1"):
        raise RuntimeError(f"Refusing to load local evaluation corpus into non-local DB_HOST={config.DB_HOST!r}")


def _parse_chunk_index(chunk_id: str, fallback: int) -> int:
    tail = chunk_id.rsplit(":", maxsplit=1)[-1]
    return int(tail) if tail.isdigit() else fallback


def _extract_section_locator(raw: dict[str, Any], chunk_index: int) -> str:
    section = str(raw.get("section") or "DOC").strip()
    return f"{section}:p{chunk_index}"[:100]


async def load_corpus_into_session(
    session: AsyncSession,
    *,
    corpus_path: Path,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
) -> tuple[int, int]:
    """Idempotently upsert active LEGACY_V1 documents and chunks from `corpus_chunks.jsonl`."""
    if not corpus_path.is_file():
        raise FileNotFoundError(f"Corpus JSONL file not found: {corpus_path}")

    raw_rows = [json.loads(line) for line in corpus_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not raw_rows:
        raise ValueError(f"Corpus JSONL file is empty: {corpus_path}")

    docs_by_url: dict[str, KnowledgeDocument] = {}
    next_index_by_doc: dict[object, int] = {}
    doc_upserts = 0
    chunk_upserts = 0

    for seq_idx, row in enumerate(raw_rows):
        chunk_id = str(row["chunk_id"]).strip()
        item_seq = str(row["item_seq"]).strip()
        item_name = str(row["item_name"]).strip()
        section = str(row["section"]).strip()
        source_code = str(row.get("source_code") or "MFDS_DATA_GO_KR_15095677_EVAL").strip()
        source_version = str(row.get("source_version") or "local-eval@1").strip()[:100]
        content = str(row["content"]).strip()
        if not chunk_id or not item_seq or not item_name or not content:
            raise ValueError(f"Invalid corpus row at index {seq_idx}")

        source_url = f"mfds://data.go.kr/15095677/{item_seq}"[:500]
        doc = docs_by_url.get(source_url)
        if doc is None:
            doc = (
                await session.execute(
                    select(KnowledgeDocument).where(
                        KnowledgeDocument.source_url == source_url,
                        KnowledgeDocument.document_version == source_version,
                    )
                )
            ).scalar_one_or_none()
            if doc is None:
                doc = KnowledgeDocument(
                    title=item_name[:500],
                    publisher=source_code[:255],
                    source_url=source_url,
                    document_version=source_version,
                    record_contract_version=KnowledgeDocumentContractVersion.LEGACY_V1,
                    document_status=KnowledgeDocumentStatus.ACTIVE,
                )
                session.add(doc)
                await session.flush()
                doc_upserts += 1
            else:
                doc.title = item_name[:500]
                doc.publisher = source_code[:255]
                doc.document_status = KnowledgeDocumentStatus.ACTIVE
            docs_by_url[source_url] = doc

        indexed_text = f"{item_name} [{section}] {content}"
        content_hash = hashlib.sha256(indexed_text.encode("utf-8")).hexdigest()
        norm_locator = _extract_section_locator(row, seq_idx)

        chunk = (
            await session.execute(select(KnowledgeChunk).where(KnowledgeChunk.vector_store_key == chunk_id))
        ).scalar_one_or_none()
        if chunk is None:
            next_index = next_index_by_doc.get(doc.id)
            if next_index is None:
                existing_order = (
                    await session.execute(
                        select(KnowledgeChunk.chunk_index)
                        .where(KnowledgeChunk.knowledge_document_id == doc.id)
                        .order_by(KnowledgeChunk.chunk_index.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                next_index = 0 if existing_order is None else int(existing_order) + 1
            next_index_by_doc[doc.id] = next_index + 1
            chunk = KnowledgeChunk(
                knowledge_document_id=doc.id,
                chunk_index=next_index,
                chunk_text=indexed_text,
                embedding_model=embedding_model,
                vector_store_key=chunk_id[:255],
                content_hash=content_hash,
                normalization_version=norm_locator,
            )
            session.add(chunk)
            chunk_upserts += 1
        else:
            chunk.chunk_text = indexed_text
            chunk.embedding_model = embedding_model
            chunk.content_hash = content_hash
            chunk.normalization_version = norm_locator

    await session.flush()
    return doc_upserts, chunk_upserts


async def _run(corpus_path: Path) -> tuple[int, int]:
    _require_local_database()
    try:
        async with AsyncSessionFactory() as session:
            docs_added, chunks_added = await load_corpus_into_session(session, corpus_path=corpus_path)
            await session.commit()
            return docs_added, chunks_added
    finally:
        await close_database()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load local MFDS eval corpus into local app DB knowledge tables.")
    parser.add_argument("--corpus-path", type=Path, default=DEFAULT_CORPUS_PATH)
    args = parser.parse_args(list(argv) if argv is not None else None)
    docs_added, chunks_added = asyncio.run(_run(args.corpus_path))
    print(f"Loaded local eval corpus: docs_added={docs_added}, chunks_added={chunks_added}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
