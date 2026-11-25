from __future__ import annotations

import hashlib
import json
import socket
import stat
import tempfile
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from ai_worker.adapters.source_artifact_owner_protocol import (
    INTENT_SCHEMA,
    MAX_CONTROL_FRAME_BYTES,
    OUTCOME_SCHEMA,
    WIRE_SCHEMA,
    CallerAttemptState,
    OutcomeObservation,
    OwnerBackedSourceArtifactReader,
    OwnerBackedSourceArtifactStore,
    OwnerClientConfig,
    OwnerServerConfig,
    PeerIdentity,
    SourceArtifactOwnerServer,
    read_control_frame,
)
from ai_worker.tasks.rag.source_ingestion.artifacts import RawArtifactMetadata

OWNER_UID = 10010
OWNER_GID = 10010
PUBLISHER_UID = 10001
PUBLISHER_GID = 10001
READER_UID = 10002
READER_GID = 10002
INTRUDER_UID = 10099
INTRUDER_GID = 10099


@contextmanager
def _short_socket_dir() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="dso-") as raw:
        path = Path(raw).resolve()
        path.chmod(0o700)
        yield path


def _meta(content: bytes, content_type: str = "application/json") -> RawArtifactMetadata:
    return RawArtifactMetadata(
        artifact_key="product-page-1.json",
        raw_checksum=hashlib.sha256(content).hexdigest(),
        byte_size=len(content),
        content_type=content_type,
    )


def _server_config(base: Path, sock_dir: Path, *, max_artifact_bytes: int = 1024 * 1024) -> OwnerServerConfig:
    return OwnerServerConfig(
        namespace="mfds-synthetic",
        socket_path=sock_dir / "owner.sock",
        artifact_root=base / "vault",
        staging_root=base / "staging",
        outcome_root=base / "outcome",
        publisher_uids=frozenset({PUBLISHER_UID}),
        reader_uids=frozenset({READER_UID}),
        expected_gid=None,
        max_artifact_bytes=max_artifact_bytes,
    )


def _client_config(base: Path, sock_dir: Path, *, max_artifact_bytes: int = 1024 * 1024) -> OwnerClientConfig:
    return OwnerClientConfig(
        namespace="mfds-synthetic",
        socket_path=sock_dir / "owner.sock",
        expected_owner_uid=OWNER_UID,
        expected_owner_gid=OWNER_GID,
        intent_root=base / "intent",
        max_artifact_bytes=max_artifact_bytes,
    )


def test_control_frame_boundary_and_json_validation() -> None:
    left, right = socket.socketpair()
    try:
        pad_len = MAX_CONTROL_FRAME_BYTES - len('{"schema":"source-artifact-owner@1","pad":""}\n')
        exact_4096 = ('{"schema":"source-artifact-owner@1","pad":"' + ("a" * pad_len) + '"}\n').encode("utf-8")
        assert len(exact_4096) == MAX_CONTROL_FRAME_BYTES
        left.sendall(exact_4096)
        parsed = read_control_frame(right)
        assert parsed["schema"] == WIRE_SCHEMA
    finally:
        left.close()
        right.close()

    # 4097 bytes -> rejected
    left, right = socket.socketpair()
    try:
        over_4097 = ('{"schema":"source-artifact-owner@1","pad":"' + ("a" * (pad_len + 1)) + '"}\n').encode("utf-8")
        assert len(over_4097) == MAX_CONTROL_FRAME_BYTES + 1
        left.sendall(over_4097)
        with pytest.raises(ValueError, match="PROTOCOL_REJECTED"):
            read_control_frame(right)
    finally:
        left.close()
        right.close()

    # Duplicate JSON key -> rejected
    left, right = socket.socketpair()
    try:
        left.sendall(b'{"schema":"source-artifact-owner@1","schema":"dup"}\n')
        with pytest.raises(ValueError, match="PROTOCOL_REJECTED"):
            read_control_frame(right)
    finally:
        left.close()
        right.close()

    # NaN -> rejected
    left, right = socket.socketpair()
    try:
        left.sendall(b'{"schema":"source-artifact-owner@1","byte_size":NaN}\n')
        with pytest.raises(ValueError, match="PROTOCOL_REJECTED"):
            read_control_frame(right)
    finally:
        left.close()
        right.close()


def test_put_read_outcome_happy_path_and_role_enforcement(tmp_path: Path) -> None:
    with _short_socket_dir() as sock_dir:
        s_cfg = _server_config(tmp_path, sock_dir)
        c_cfg = _client_config(tmp_path, sock_dir)
        current_peer = PeerIdentity(PUBLISHER_UID, PUBLISHER_GID)

        server = SourceArtifactOwnerServer(
            s_cfg,
            peer_cred_getter=lambda _conn: current_peer,
            owner_identity=PeerIdentity(OWNER_UID, OWNER_GID),
        )
        reader = OwnerBackedSourceArtifactReader(
            c_cfg,
            peer_cred_getter=lambda _conn: PeerIdentity(OWNER_UID, OWNER_GID),
        )
        store = OwnerBackedSourceArtifactStore(
            c_cfg,
            reader=reader,
            peer_cred_getter=lambda _conn: PeerIdentity(OWNER_UID, OWNER_GID),
        )
        assert store.supports_transaction_cleanup is False

        stop_event = threading.Event()

        def _serve() -> None:
            while not stop_event.is_set():
                server.serve_once(timeout=0.05)

        worker = threading.Thread(target=_serve, daemon=True)
        worker.start()
        try:
            content = b'{"header":{"resultCode":"00"},"body":{"items":[]}}'
            src = tmp_path / "page1.json"
            src.write_bytes(content)
            meta = _meta(content)

            stored = store.put_verified(page_number=1, file_path=src, metadata=meta)
            expected_key = f"sha256/{meta.raw_checksum[:2]}/{meta.raw_checksum}.artifact"
            assert stored.object_key == expected_key
            assert len(store.finalized) == 1

            # Verify vault permissions and journals
            vault_file = s_cfg.artifact_root / expected_key
            assert vault_file.read_bytes() == content
            assert stat.S_IMODE(vault_file.stat().st_mode) == 0o600
            assert stat.S_IMODE(s_cfg.artifact_root.stat().st_mode) == 0o700

            attempt_id = store.last_attempt_id
            assert attempt_id is not None
            obs = store.query_outcome(attempt_id)
            assert obs == OutcomeObservation(
                attempt_id=attempt_id,
                generation=server.generation,
                result="COMPLETE",
                reason=None,
                object_key=expected_key,
                checksum=meta.raw_checksum,
                byte_size=len(content),
                content_type="application/json",
            )

            # Reader UID can READ, but cannot PUT or OUTCOME
            current_peer = PeerIdentity(READER_UID, READER_GID)
            assert reader.read_verified(object_key=expected_key, metadata=meta) == content

            with pytest.raises(ValueError, match="AUTH_REJECTED"):
                store.put_verified(page_number=1, file_path=src, metadata=meta)

            with pytest.raises(ValueError, match="AUTH_REJECTED"):
                store.query_outcome(attempt_id)

            # Unregistered UID rejected even on READ
            current_peer = PeerIdentity(INTRUDER_UID, INTRUDER_GID)
            with pytest.raises(ValueError, match="AUTH_REJECTED"):
                reader.read_verified(object_key=expected_key, metadata=meta)
        finally:
            stop_event.set()
            worker.join(timeout=2.0)
            server.close()


def test_delete_op_and_size_mismatch_rejected(tmp_path: Path) -> None:
    with _short_socket_dir() as sock_dir:
        s_cfg = _server_config(tmp_path, sock_dir)
        server = SourceArtifactOwnerServer(
            s_cfg,
            peer_cred_getter=lambda _conn: PeerIdentity(PUBLISHER_UID, PUBLISHER_GID),
            owner_identity=PeerIdentity(OWNER_UID, OWNER_GID),
        )
        try:
            # 1. DELETE op is rejected with PROTOCOL_REJECTED
            client_sock = server.open_client_socket()
            client_sock.sendall(
                json.dumps(
                    {
                        "schema": WIRE_SCHEMA,
                        "op": "DELETE",
                        "namespace": "mfds-synthetic",
                        "checksum": "a" * 64,
                    }
                ).encode("utf-8")
                + b"\n"
            )
            client_sock.shutdown(socket.SHUT_WR)
            server.serve_once(timeout=1.0)
            reply = read_control_frame(client_sock)
            client_sock.close()
            assert reply["result"] == "REJECTED"
            assert reply["reason"] == "PROTOCOL_REJECTED"

            # 2. Declared byte_size + 1 / - 1 rejected with INPUT_MISMATCH and staging cleaned
            for declared_size, body in ((5, b"1234"), (5, b"123456")):
                attempt_id = str(uuid.uuid4())
                checksum = hashlib.sha256(b"12345").hexdigest()
                client_sock = server.open_client_socket()

                t = threading.Thread(target=lambda: server.serve_once(timeout=1.0))
                t.start()
                client_sock.sendall(
                    json.dumps(
                        {
                            "schema": WIRE_SCHEMA,
                            "op": "PUT",
                            "namespace": "mfds-synthetic",
                            "attempt_id": attempt_id,
                            "checksum": checksum,
                            "byte_size": declared_size,
                            "content_type": "text/plain",
                        }
                    ).encode("utf-8")
                    + b"\n"
                )
                accepted = read_control_frame(client_sock)
                assert accepted["result"] == "ACCEPTED"
                client_sock.sendall(body)
                client_sock.shutdown(socket.SHUT_WR)
                terminal = read_control_frame(client_sock)
                client_sock.close()
                t.join(timeout=2.0)

                assert terminal["result"] == "REJECTED"
                assert terminal["reason"] == "INPUT_MISMATCH"
                assert list(s_cfg.staging_root.iterdir()) == []
        finally:
            server.close()


def test_prepared_local_refuses_outcome_and_lost_terminal_reply_preserves_coexistence(tmp_path: Path) -> None:
    with _short_socket_dir() as sock_dir:
        s_cfg = _server_config(tmp_path, sock_dir)
        c_cfg = _client_config(tmp_path, sock_dir)

        # Case A: PREPARED_LOCAL (intent prepared.json written, no accepted.json) -> query_outcome refused locally
        store = OwnerBackedSourceArtifactStore(
            c_cfg,
            peer_cred_getter=lambda _conn: PeerIdentity(OWNER_UID, OWNER_GID),
        )
        unaccepted_attempt = str(uuid.uuid4())
        intent_dir = (c_cfg.intent_root or (tmp_path / "intent")) / c_cfg.namespace / unaccepted_attempt
        intent_dir.mkdir(parents=True, mode=0o700)
        (intent_dir / "prepared.json").write_text(
            json.dumps(
                {
                    "schema": INTENT_SCHEMA,
                    "event": "PREPARED",
                    "namespace": c_cfg.namespace,
                    "attempt_id": unaccepted_attempt,
                    "checksum": "b" * 64,
                    "byte_size": 4,
                    "content_type": "text/plain",
                }
            ),
            encoding="utf-8",
        )
        assert store.get_caller_attempt_state(unaccepted_attempt) is CallerAttemptState.PREPARED_LOCAL
        with pytest.raises(ValueError, match="durable accepted generation"):
            store.query_outcome(unaccepted_attempt)

        # Case B: Owner writes durable TERMINAL COMPLETE, then drops connection before sending terminal frame
        server = SourceArtifactOwnerServer(
            s_cfg,
            peer_cred_getter=lambda _conn: PeerIdentity(PUBLISHER_UID, PUBLISHER_GID),
            drop_reply_after_terminal=True,
            owner_identity=PeerIdentity(OWNER_UID, OWNER_GID),
        )
        stop_event = threading.Event()

        def _serve() -> None:
            while not stop_event.is_set():
                server.serve_once(timeout=0.05)

        worker = threading.Thread(target=_serve, daemon=True)
        worker.start()
        try:
            content = b"durable-complete-lost-reply"
            src = tmp_path / "lost.txt"
            src.write_bytes(content)
            meta = _meta(content, content_type="text/plain")

            with pytest.raises(ValueError, match="CLIENT_TRANSPORT_UNKNOWN"):
                store.put_verified(page_number=1, file_path=src, metadata=meta)

            # Not added to store.finalized when reply was lost
            assert store.finalized == []
            attempt_id = store.last_attempt_id
            assert attempt_id is not None

            # Caller observed.json has INDETERMINATE / CLIENT_TRANSPORT_UNKNOWN
            caller_observed_path = (
                (c_cfg.intent_root or (tmp_path / "intent")) / c_cfg.namespace / attempt_id / "observed.json"
            )
            caller_observed = json.loads(caller_observed_path.read_text(encoding="utf-8"))
            assert caller_observed["schema"] == INTENT_SCHEMA
            assert caller_observed["result"] == "INDETERMINATE"
            assert caller_observed["reason"] == "CLIENT_TRANSPORT_UNKNOWN"

            # Owner terminal.json has COMPLETE
            owner_terminal_path = s_cfg.outcome_root / s_cfg.namespace / attempt_id / "terminal.json"
            owner_terminal = json.loads(owner_terminal_path.read_text(encoding="utf-8"))
            assert owner_terminal["schema"] == OUTCOME_SCHEMA
            assert owner_terminal["result"] == "COMPLETE"

            # Querying OUTCOME with the known generation returns COMPLETE without mutating caller observed.json
            server.drop_reply_after_terminal = False
            obs = store.query_outcome(attempt_id)
            assert obs.result == "COMPLETE"
            assert obs.generation == server.generation
            assert json.loads(caller_observed_path.read_text(encoding="utf-8")) == caller_observed

            # Restarting owner with a new generation returns UNKNOWN_GENERATION for the old expected_generation
            stop_event.set()
            worker.join(timeout=2.0)
            server.close()

            server2 = SourceArtifactOwnerServer(
                s_cfg,
                peer_cred_getter=lambda _conn: PeerIdentity(PUBLISHER_UID, PUBLISHER_GID),
                owner_identity=PeerIdentity(OWNER_UID, OWNER_GID),
            )
            t2 = threading.Thread(target=lambda: server2.serve_once(timeout=1.0))
            t2.start()
            try:
                obs2 = store.query_outcome(attempt_id)
                assert obs2.result == "UNKNOWN_GENERATION"
                assert obs2.reason == "GENERATION_UNKNOWN"
            finally:
                t2.join(timeout=2.0)
                server2.close()
        finally:
            stop_event.set()
            worker.join(timeout=2.0)
            server.close()


def test_trailing_bytes_limit_rejected_and_outcome_vault_corruption_indeterminate(tmp_path: Path) -> None:
    with _short_socket_dir() as sock_dir:
        s_cfg = OwnerServerConfig(
            namespace="mfds.synthetic",
            socket_path=sock_dir / "owner.sock",
            artifact_root=tmp_path / "vault",
            staging_root=tmp_path / "staging",
            outcome_root=tmp_path / "outcome",
            publisher_uids=frozenset({PUBLISHER_UID}),
            reader_uids=frozenset({READER_UID}),
            max_journal_attempts=1,
        )
        c_cfg = OwnerClientConfig(
            namespace="mfds.synthetic",
            socket_path=sock_dir / "owner.sock",
            expected_owner_uid=OWNER_UID,
            expected_owner_gid=OWNER_GID,
            intent_root=tmp_path / "intent",
        )
        server = SourceArtifactOwnerServer(
            s_cfg,
            peer_cred_getter=lambda _conn: PeerIdentity(PUBLISHER_UID, PUBLISHER_GID),
            owner_identity=PeerIdentity(OWNER_UID, OWNER_GID),
        )
        store = OwnerBackedSourceArtifactStore(
            c_cfg,
            peer_cred_getter=lambda _conn: PeerIdentity(OWNER_UID, OWNER_GID),
        )
        stop_event = threading.Event()

        def _serve() -> None:
            while not stop_event.is_set():
                server.serve_once(timeout=0.05)

        worker = threading.Thread(target=_serve, daemon=True)
        worker.start()
        try:
            # 1. Trailing bytes after READ control frame -> PROTOCOL_REJECTED
            raw_sock = server.open_client_socket()
            raw_sock.sendall(
                json.dumps(
                    {
                        "schema": WIRE_SCHEMA,
                        "op": "READ",
                        "namespace": "mfds.synthetic",
                        "object_key": "sha256/aa/" + ("a" * 64) + ".artifact",
                        "checksum": "a" * 64,
                        "byte_size": 4,
                        "content_type": "text/plain",
                    }
                ).encode()
                + b"\nextra"
            )
            raw_sock.shutdown(socket.SHUT_WR)
            trailing_reply = read_control_frame(raw_sock)
            raw_sock.close()
            assert trailing_reply["result"] == "REJECTED"
            assert trailing_reply["reason"] == "PROTOCOL_REJECTED"

            # 2. First PUT succeeds; second PUT with new attempt_id exceeds max_journal_attempts=1 -> LIMIT_REJECTED
            content = b"first-object"
            src = tmp_path / "first.txt"
            src.write_bytes(content)
            meta = _meta(content, content_type="text/plain")
            stored = store.put_verified(page_number=1, file_path=src, metadata=meta)
            first_attempt = store.last_attempt_id
            assert first_attempt is not None

            with pytest.raises(ValueError, match="LIMIT_REJECTED"):
                store.put_verified(page_number=1, file_path=src, metadata=meta)

            # 3. Corrupt vault file after COMPLETE -> OUTCOME returns INDETERMINATE / IMMUTABLE_OBJECT_MISMATCH
            vault_file = s_cfg.artifact_root / stored.object_key
            vault_file.chmod(0o600)
            vault_file.write_bytes(b"corrupted-vault-bytes")
            obs = store.query_outcome(first_attempt)
            assert obs.result == "INDETERMINATE"
            assert obs.reason == "IMMUTABLE_OBJECT_MISMATCH"
        finally:
            stop_event.set()
            worker.join(timeout=2.0)
            server.close()
