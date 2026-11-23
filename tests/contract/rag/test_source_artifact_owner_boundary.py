"""End-to-end synthetic contract test for source-artifact-owner@1 across distinct role UIDs."""

from __future__ import annotations

import hashlib
import json
import stat
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from ai_worker.adapters.source_artifact_owner_protocol import (
    OwnerBackedSourceArtifactReader,
    OwnerBackedSourceArtifactStore,
    OwnerClientConfig,
    OwnerServerConfig,
    PeerIdentity,
    SourceArtifactOwnerServer,
)
from ai_worker.admin.mfds_product_rematerialization import prepare_product_rematerialization
from ai_worker.tasks.rag.catalog.mfds_product_source import ProductSourceRepository, read_product_input
from ai_worker.tasks.rag.knowledge_materialization import (
    CHUNK_POLICY_VERSION,
    KnowledgeMaterializationRequest,
    materialize_documents,
)
from ai_worker.tasks.rag.source_ingestion.artifacts import RawArtifactMetadata, StoredRawArtifact
from ai_worker.tasks.rag.source_ingestion.checksums import product_canonical_checksum, raw_manifest_checksum
from ai_worker.tasks.rag.source_ingestion.mfds_label import (
    OBSERVED_CONTENT_TYPE,
    IngestionArtifactReceipt,
)
from ai_worker.tasks.rag.source_ingestion.receipt_validation import calculate_endpoint_receipt_hash
from ai_worker.tasks.rag.source_ingestion.snapshot_lifecycle import SnapshotVerificationStatus
from ai_worker.tests.rag.source_ingestion.test_mfds_product import REPOSITORY_RECEIPT, envelope, product
from ai_worker.tests.rag.test_knowledge_materialization import _make_source_doc

OWNER_UID = 10010
OWNER_GID = 10010
PUBLISHER_UID = 10001
PUBLISHER_GID = 10001
READER_UID = 10002
READER_GID = 10002
CLEANUP_UID = 10003
CLEANUP_GID = 10003


@contextmanager
def _short_socket_dir() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="dso-e2e-") as raw:
        path = Path(raw).resolve()
        path.chmod(0o700)
        yield path


def _build_product_receipt_and_manifest(
    tmp_path: Path, *, object_key: str, checksum: str, byte_size: int, row: dict[str, object]
) -> tuple[Path, Path]:
    receipt_payload = json.loads(REPOSITORY_RECEIPT.read_text(encoding="utf-8"))
    receipt_payload["validated_record_count"] = 1
    fixtures = []
    for index, (scenario, content) in enumerate(
        (
            ("LIST_APPROVED_PRODUCTS_SUCCESS", envelope([product()])),
            ("SYNTHETIC_AUTH_FAILURE", envelope([], code="30")),
            ("SYNTHETIC_DAILY_LIMIT", envelope([], code="22")),
            ("SYNTHETIC_EMPTY", envelope([])),
            ("SYNTHETIC_SCHEMA_DRIFT", {"invalid": True}),
        )
    ):
        relative = f"tests/fixtures/rag/mfds/e2e-product-{index}.json"
        fixture_path = tmp_path / relative
        fixture_path.parent.mkdir(parents=True, exist_ok=True)
        fixture_path.write_text(json.dumps(content), encoding="utf-8")
        fixtures.append(
            {
                "scenario": scenario,
                "path": relative,
                "sha256": hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
            }
        )
    receipt_payload["fixture_evidence"] = fixtures
    receipt_payload["receipt_hash"] = calculate_endpoint_receipt_hash(receipt_payload)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt_payload), encoding="utf-8")

    canonical = product_canonical_checksum([row])
    meta = RawArtifactMetadata("product-page-1.json", checksum, byte_size, "application/json")
    finished = "2026-09-19T03:34:30.340915Z"
    manifest_payload = {
        "schema": "mfds-product-rematerialization-manifest@1",
        "source_code": "MFDS_PRODUCT_APPROVAL",
        "endpoint_code": "MFDS_PRODUCT_APPROVAL_API",
        "operation_code": "LIST_APPROVED_PRODUCTS",
        "source_version": f"api:{finished}:{canonical}",
        "canonical_checksum": canonical,
        "raw_manifest_checksum": raw_manifest_checksum([meta]),
        "endpoint_receipt_hash": receipt_payload["receipt_hash"],
        "record_count": 1,
        "started_at": "2026-09-19T03:31:03.856732Z",
        "finished_at": finished,
        "artifacts": [
            {
                "page_number": 1,
                "artifact_key": "product-page-1.json",
                "object_key": object_key,
                "raw_checksum": checksum,
                "byte_size": byte_size,
                "content_type": "application/json",
            }
        ],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
    return manifest_path, receipt_path


class _InMemoryProductSourceRepo(ProductSourceRepository):
    def __init__(
        self,
        *,
        snapshot: Any,
        run: Any,
        artifacts: tuple[IngestionArtifactReceipt, ...],
    ) -> None:
        self._snapshot = snapshot
        self._run = run
        self._artifacts = artifacts

    async def get_snapshot_receipt(self, *, snapshot_id: UUID) -> Any:
        return self._snapshot if snapshot_id == self._snapshot.source_snapshot_id else None

    async def get_attempt_receipt(self, *, ingestion_run_id: UUID) -> Any:
        return self._run if ingestion_run_id == self._run.ingestion_run_id else None

    async def get_ingestion_artifact_receipts(self, *, ingestion_run_id: UUID) -> tuple[IngestionArtifactReceipt, ...]:
        return self._artifacts if ingestion_run_id == self._run.ingestion_run_id else ()


@pytest.mark.asyncio
async def test_end_to_end_owner_boundary_across_publisher_remat_catalog_knowledge_and_cleanup(
    tmp_path: Path,
) -> None:
    with _short_socket_dir() as sock_dir:
        server_cfg = OwnerServerConfig(
            namespace="mfds-e2e",
            socket_path=sock_dir / "owner.sock",
            artifact_root=tmp_path / "owner-vault",
            staging_root=tmp_path / "owner-staging",
            outcome_root=tmp_path / "owner-outcome",
            publisher_uids=frozenset({PUBLISHER_UID}),
            reader_uids=frozenset({READER_UID}),
        )
        publisher_cfg = OwnerClientConfig(
            namespace="mfds-e2e",
            socket_path=sock_dir / "owner.sock",
            expected_owner_uid=OWNER_UID,
            expected_owner_gid=OWNER_GID,
            intent_root=tmp_path / "publisher-intent",
        )
        reader_cfg = OwnerClientConfig(
            namespace="mfds-e2e",
            socket_path=sock_dir / "owner.sock",
            expected_owner_uid=OWNER_UID,
            expected_owner_gid=OWNER_GID,
            intent_root=tmp_path / "reader-intent",
        )
        cleanup_cfg = OwnerClientConfig(
            namespace="mfds-e2e",
            socket_path=sock_dir / "owner.sock",
            expected_owner_uid=OWNER_UID,
            expected_owner_gid=OWNER_GID,
            intent_root=tmp_path / "cleanup-intent",
        )

        current_peer = PeerIdentity(PUBLISHER_UID, PUBLISHER_GID)
        server = SourceArtifactOwnerServer(
            server_cfg,
            peer_cred_getter=lambda _conn: current_peer,
            owner_identity=PeerIdentity(OWNER_UID, OWNER_GID),
        )
        stop_event = threading.Event()

        def _serve() -> None:
            while not stop_event.is_set():
                server.serve_once(timeout=0.05)

        worker = threading.Thread(target=_serve, daemon=True)
        worker.start()
        try:
            pub_reader = OwnerBackedSourceArtifactReader(
                publisher_cfg,
                peer_cred_getter=lambda _conn: PeerIdentity(OWNER_UID, OWNER_GID),
            )
            pub_store = OwnerBackedSourceArtifactStore(
                publisher_cfg,
                reader=pub_reader,
                peer_cred_getter=lambda _conn: PeerIdentity(OWNER_UID, OWNER_GID),
            )

            # 1. Publisher PUT + post-commit verified READ for Product JSON & Label XML (EE, UD, NB)
            prod_row = product("199900001")
            prod_bytes = json.dumps(
                envelope([prod_row], page=1, total=1), ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            prod_src = tmp_path / "prod-page-1.json"
            prod_src.write_bytes(prod_bytes)
            prod_meta = RawArtifactMetadata(
                artifact_key="product-page-1.json",
                raw_checksum=hashlib.sha256(prod_bytes).hexdigest(),
                byte_size=len(prod_bytes),
                content_type="application/json",
            )
            stored_prod = pub_store.put_verified(page_number=1, file_path=prod_src, metadata=prod_meta)

            label_sections: dict[str, tuple[StoredRawArtifact, RawArtifactMetadata, bytes]] = {}
            for sec, title in (("EE", "효능효과"), ("UD", "용법용량"), ("NB", "사용상의주의사항")):
                xml_bytes = (
                    f'<DOC type="{sec}" title="{title}">'
                    f'<ARTICLE title="{title}"><PARAGRAPH>합성 검증 문구 {sec}</PARAGRAPH></ARTICLE>'
                    f"</DOC>"
                ).encode()
                xml_src = tmp_path / f"{sec}.xml"
                xml_src.write_bytes(xml_bytes)
                xml_meta = RawArtifactMetadata(
                    artifact_key=f"mfds-label/199900001/{sec}.xml",
                    raw_checksum=hashlib.sha256(xml_bytes).hexdigest(),
                    byte_size=len(xml_bytes),
                    content_type=OBSERVED_CONTENT_TYPE,
                )
                stored_xml = pub_store.put_verified(page_number=1, file_path=xml_src, metadata=xml_meta)
                label_sections[sec] = (stored_xml, xml_meta, xml_bytes)

            # 2. Publisher rematerialization via OwnerBackedSourceArtifactReader + transient spool
            # Make an unreachable vault path to prove rematerialization does not open the vault directly
            unreachable_vault = tmp_path / "no-direct-vault-access"
            unreachable_vault.mkdir(mode=0o000)
            manifest_path, receipt_path = _build_product_receipt_and_manifest(
                tmp_path,
                object_key=stored_prod.object_key,
                checksum=prod_meta.raw_checksum,
                byte_size=prod_meta.byte_size,
                row=prod_row,
            )
            spool_root = tmp_path / "remat-spool"
            try:
                prepared = prepare_product_rematerialization(
                    manifest_path=manifest_path,
                    receipt_path=receipt_path,
                    repository_root=tmp_path,
                    artifact_root=unreachable_vault,
                    reader=pub_reader,
                    spool_root=spool_root,
                )
                try:
                    page_num, staged_path, staged_meta = prepared.acquisition.artifacts[0]
                    assert stat.S_IMODE(staged_path.stat().st_mode) == 0o600
                    remat_stored = prepared.artifact_store.put_verified(
                        page_number=page_num,
                        file_path=staged_path,
                        metadata=staged_meta,
                    )
                    assert remat_stored.object_key == stored_prod.object_key
                finally:
                    prepared.cleanup()
                assert list(spool_root.iterdir()) == []
            finally:
                unreachable_vault.chmod(0o700)

            # 3. Consumer role (READER_UID): READ succeeds for Catalog & Knowledge, PUT and OUTCOME fail closed
            current_peer = PeerIdentity(READER_UID, READER_GID)
            consumer_reader = OwnerBackedSourceArtifactReader(
                reader_cfg,
                peer_cred_getter=lambda _conn: PeerIdentity(OWNER_UID, OWNER_GID),
            )
            consumer_store = OwnerBackedSourceArtifactStore(
                reader_cfg,
                reader=consumer_reader,
                peer_cred_getter=lambda _conn: PeerIdentity(OWNER_UID, OWNER_GID),
            )

            snap_id = UUID("00000000-0000-4000-8000-000000000101")
            run_id = UUID("00000000-0000-4000-8000-000000000102")
            op_id = UUID("00000000-0000-4000-8000-000000000103")
            ep_id = UUID("00000000-0000-4000-8000-000000000104")
            src_id = UUID("00000000-0000-4000-8000-000000000105")
            now = datetime(2026, 11, 12, 0, 0, tzinfo=UTC)

            repo = _InMemoryProductSourceRepo(
                snapshot=SimpleNamespace(
                    source_snapshot_id=snap_id,
                    source_id=src_id,
                    source_code="MFDS_PRODUCT_APPROVAL",
                    endpoint_id=ep_id,
                    endpoint_code="MFDS_PRODUCT_APPROVAL_API",
                    operation_id=op_id,
                    operation_code="LIST_APPROVED_PRODUCTS",
                    source_version="api:2026-09-19T03:34:30.340915Z:" + ("a" * 64),
                    canonical_checksum=product_canonical_checksum([prod_row]),
                    canonicalization_spec_version="mfds-product-approval@1",
                    endpoint_receipt_hash="b" * 64,
                    verification_status=SnapshotVerificationStatus.CURRENT,
                    record_count=1,
                    rejected_record_count=0,
                    collected_at=now,
                    verified_at=now,
                    verified_by="synthetic-operator",
                    validate_provenance=lambda: None,
                ),
                run=SimpleNamespace(
                    ingestion_run_id=run_id,
                    operation_id=op_id,
                    snapshot_id=snap_id,
                    run_status="SUCCEEDED",
                    failure_code=None,
                ),
                artifacts=(
                    IngestionArtifactReceipt(
                        ingestion_artifact_id=UUID("00000000-0000-4000-8000-000000000106"),
                        ingestion_run_id=run_id,
                        page_number=1,
                        artifact_key=prod_meta.artifact_key,
                        storage_backend="LOCAL_PRIVATE",
                        object_key=stored_prod.object_key,
                        raw_checksum=prod_meta.raw_checksum,
                        byte_size=prod_meta.byte_size,
                        content_type=prod_meta.content_type,
                    ),
                ),
            )
            cat_input, _ = await read_product_input(
                repository=repo,
                artifact_reader=consumer_reader,
                source_snapshot_id=str(snap_id),
                ingestion_run_id=str(run_id),
                item_seq="199900001",
            )
            assert cat_input.canonical_code == "199900001"

            # Knowledge materialization over consumer_reader
            label_snap_id = UUID("00000000-0000-4000-8000-000000000201")
            member_ids = (
                UUID("00000000-0000-4000-8000-000000000211"),
                UUID("00000000-0000-4000-8000-000000000212"),
                UUID("00000000-0000-4000-8000-000000000213"),
            )
            source_docs = tuple(
                _make_source_doc(
                    section=sec,
                    raw_bytes=label_sections[sec][2],
                    item_seq="199900001",
                    snapshot_id=label_snap_id,
                    member_id=member_id,
                )
                for sec, member_id in zip(("EE", "UD", "NB"), member_ids, strict=True)
            )
            drafts = materialize_documents(
                request=KnowledgeMaterializationRequest(
                    snapshot_id=label_snap_id,
                    member_ids=member_ids,
                    expected_item_seq="199900001",
                    chunk_policy_version=CHUNK_POLICY_VERSION,
                ),
                source_docs=source_docs,
                artifact_reader=consumer_reader,
            )
            assert len(drafts) == 3

            # Consumer PUT and OUTCOME rejected with AUTH_REJECTED
            with pytest.raises(ValueError, match="AUTH_REJECTED"):
                consumer_store.put_verified(page_number=1, file_path=prod_src, metadata=prod_meta)

            assert pub_store.last_attempt_id is not None
            with pytest.raises(ValueError, match="AUTH_REJECTED"):
                pub_store.query_outcome(pub_store.last_attempt_id)

            # 4. Cleanup executor role (CLEANUP_UID): rejected by Owner UDS and has no vault access
            current_peer = PeerIdentity(CLEANUP_UID, CLEANUP_GID)
            cleanup_reader = OwnerBackedSourceArtifactReader(
                cleanup_cfg,
                peer_cred_getter=lambda _conn: PeerIdentity(OWNER_UID, OWNER_GID),
            )
            with pytest.raises(ValueError, match="AUTH_REJECTED"):
                cleanup_reader.read_verified(object_key=stored_prod.object_key, metadata=prod_meta)
        finally:
            stop_event.set()
            worker.join(timeout=2.0)
            server.close()
