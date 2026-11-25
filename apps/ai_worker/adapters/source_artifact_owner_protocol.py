"""Source Artifact Owner Unix domain socket 프로토콜 및 저널 구현 (source-artifact-owner@1)."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import select
import socket
import stat
import struct
import tempfile
import threading
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from ai_worker.adapters.local_private_source_artifact_store import LocalPrivateSourceArtifactStore
from ai_worker.tasks.rag.source_ingestion.artifacts import (
    IngestionArtifactKind,
    RawArtifactMetadata,
    StoredRawArtifact,
    validate_artifact_binding,
    verify_raw_artifact,
)

WIRE_SCHEMA = "source-artifact-owner@1"
INTENT_SCHEMA = "source-artifact-intent@1"
OUTCOME_SCHEMA = "source-artifact-outcome@1"
MAX_CONTROL_FRAME_BYTES = 4096
DEFAULT_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_STAGING_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_JOURNAL_ATTEMPTS = 10_000
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_ALLOWED_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "application/json",
        "application/xml",
        "text/xml",
        "text/plain",
        "application/download; UTF-8; charset=UTF-8",
    }
)
_STORAGE_BACKEND = "LOCAL_PRIVATE"
_CHUNK_SIZE = 64 * 1024

_NAMESPACE_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_CHECKSUM_RE = re.compile(r"[0-9a-f]{64}\Z")
_OBJECT_KEY_RE = re.compile(r"sha256/([0-9a-f]{2})/([0-9a-f]{64})\.artifact\Z")
_CONTENT_TYPE_RE = re.compile(r"[\x21-\x7e]+(?: [\x21-\x7e]+)*\Z")


@dataclass(frozen=True, slots=True)
class PeerIdentity:
    uid: int
    gid: int


class CallerAttemptState(StrEnum):
    PREPARED_LOCAL = "PREPARED_LOCAL"
    ACCEPTED = "ACCEPTED"
    COMPLETE = "COMPLETE"
    REJECTED = "REJECTED"
    INDETERMINATE = "INDETERMINATE"
    NOT_OBSERVED = "NOT_OBSERVED"
    UNKNOWN_GENERATION = "UNKNOWN_GENERATION"


@dataclass(frozen=True, slots=True)
class OutcomeObservation:
    attempt_id: str
    generation: str | None
    result: str
    reason: str | None
    object_key: str | None = None
    checksum: str | None = None
    byte_size: int | None = None
    content_type: str | None = None


@dataclass(frozen=True, slots=True)
class OwnerServerConfig:
    namespace: str
    socket_path: Path = field(repr=False)
    artifact_root: Path = field(repr=False)
    staging_root: Path = field(repr=False)
    outcome_root: Path = field(repr=False)
    publisher_uids: frozenset[int]
    reader_uids: frozenset[int]
    expected_gid: int | None = None
    max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES
    max_staging_bytes: int = DEFAULT_MAX_STAGING_BYTES
    max_journal_attempts: int = DEFAULT_MAX_JOURNAL_ATTEMPTS
    allowed_content_types: frozenset[str] = DEFAULT_ALLOWED_CONTENT_TYPES

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> OwnerServerConfig:
        forbidden = (
            "SOURCE_WRITER_PASSWORD",
            "SOURCE_PUBLISHER_PASSWORD",
            "SOURCE_CLEANUP_EXECUTOR_PASSWORD",
            "DB_PASSWORD",
            "DB_ADMIN_PASSWORD",
            "RAG_MFDS_API_KEY",
        )
        if any(env.get(key) for key in forbidden):
            raise ValueError("Source artifact owner requires an isolated environment.")
        namespace = env.get("SOURCE_ARTIFACT_OWNER_NAMESPACE", "").strip()
        socket_raw = env.get("SOURCE_ARTIFACT_OWNER_SOCKET", "").strip()
        artifact_raw = env.get("SOURCE_ARTIFACT_LOCAL_ROOT", "").strip()
        staging_raw = env.get("SOURCE_ARTIFACT_FINALIZER_STAGING_ROOT", "").strip()
        outcome_raw = env.get("SOURCE_ARTIFACT_OWNER_OUTCOME_ROOT", "").strip()
        pub_raw = env.get("SOURCE_ARTIFACT_OWNER_PUBLISHER_UIDS", "").strip()
        read_raw = env.get("SOURCE_ARTIFACT_OWNER_READER_UIDS", "").strip()
        if not (namespace and socket_raw and artifact_raw and staging_raw and outcome_raw):
            raise ValueError("Source artifact owner configuration is incomplete.")
        publisher_uids = frozenset(int(item) for item in pub_raw.split(",") if item.strip())
        reader_uids = frozenset(int(item) for item in read_raw.split(",") if item.strip())
        expected_gid_raw = env.get("SOURCE_ARTIFACT_OWNER_EXPECTED_GID", "").strip()
        expected_gid = int(expected_gid_raw) if expected_gid_raw else None
        max_bytes_raw = env.get("SOURCE_ARTIFACT_OWNER_MAX_BYTES", "").strip()
        max_bytes = int(max_bytes_raw) if max_bytes_raw else DEFAULT_MAX_ARTIFACT_BYTES
        return cls(
            namespace=namespace,
            socket_path=Path(socket_raw),
            artifact_root=Path(artifact_raw),
            staging_root=Path(staging_raw),
            outcome_root=Path(outcome_raw),
            publisher_uids=publisher_uids,
            reader_uids=reader_uids,
            expected_gid=expected_gid,
            max_artifact_bytes=max_bytes,
        )


@dataclass(frozen=True, slots=True)
class OwnerClientConfig:
    namespace: str
    socket_path: Path = field(repr=False)
    expected_owner_uid: int
    expected_owner_gid: int | None = None
    intent_root: Path | None = field(default=None, repr=False)
    max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    allowed_content_types: frozenset[str] = DEFAULT_ALLOWED_CONTENT_TYPES

    @classmethod
    def from_environment(cls, env: Mapping[str, str], *, require_intent_root: bool = False) -> OwnerClientConfig:
        namespace = env.get("SOURCE_ARTIFACT_OWNER_NAMESPACE", "mfds").strip()
        socket_raw = env.get("SOURCE_ARTIFACT_OWNER_SOCKET", "").strip()
        owner_uid_raw = env.get("SOURCE_ARTIFACT_OWNER_UID", "").strip()
        owner_gid_raw = env.get("SOURCE_ARTIFACT_OWNER_GID", "").strip()
        intent_raw = env.get("SOURCE_ARTIFACT_INTENT_ROOT", "").strip()
        if not socket_raw or not owner_uid_raw:
            raise ValueError("Source artifact owner client configuration requires socket and owner UID.")
        if require_intent_root and not intent_raw:
            raise ValueError("Source artifact publisher configuration requires SOURCE_ARTIFACT_INTENT_ROOT.")
        return cls(
            namespace=namespace,
            socket_path=Path(socket_raw),
            expected_owner_uid=int(owner_uid_raw),
            expected_owner_gid=int(owner_gid_raw) if owner_gid_raw else None,
            intent_root=Path(intent_raw) if intent_raw else None,
        )


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result or _has_control_chars(key):
            raise ValueError("PROTOCOL_REJECTED")
        if isinstance(value, str) and _has_control_chars(value):
            raise ValueError("PROTOCOL_REJECTED")
        result[key] = value
    return result


def _reject_nonfinite_constant(_token: str) -> Any:
    raise ValueError("PROTOCOL_REJECTED")


def read_control_frame(sock: socket.socket) -> dict[str, Any]:
    """단일 newline-delimited UTF-8 JSON 제어 프레임(<= 4096 bytes)을 읽고 검증합니다."""
    buffer = bytearray()
    while len(buffer) < MAX_CONTROL_FRAME_BYTES:
        chunk = sock.recv(1)
        if not chunk:
            raise ValueError("PROTOCOL_REJECTED")
        buffer.extend(chunk)
        if chunk == b"\n":
            break
    else:
        raise ValueError("PROTOCOL_REJECTED")

    try:
        raw_text = buffer[:-1].decode("utf-8")
        if _has_control_chars(raw_text):
            raise ValueError("PROTOCOL_REJECTED")
        parsed = json.loads(
            raw_text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ValueError("PROTOCOL_REJECTED") from None

    if not isinstance(parsed, dict) or parsed.get("schema") != WIRE_SCHEMA:
        raise ValueError("PROTOCOL_REJECTED")
    return parsed


def write_control_frame(sock: socket.socket, payload: Mapping[str, Any]) -> None:
    """단일 newline-delimited UTF-8 JSON 제어 프레임을 전송합니다."""
    for key, value in payload.items():
        if _has_control_chars(str(key)) or (isinstance(value, str) and _has_control_chars(value)):
            raise ValueError("PROTOCOL_REJECTED")
    encoded = (json.dumps(dict(payload), separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > MAX_CONTROL_FRAME_BYTES:
        raise ValueError("PROTOCOL_REJECTED")
    sock.sendall(encoded)


def _get_peer_identity(sock: socket.socket) -> PeerIdentity:
    getpeereid = getattr(os, "getpeereid", None)
    if callable(getpeereid):
        uid, gid = getpeereid(sock.fileno())
        return PeerIdentity(uid=int(uid), gid=int(gid))
    so_peercred = getattr(socket, "SO_PEERCRED", None)
    if so_peercred is not None:
        creds = sock.getsockopt(socket.SOL_SOCKET, so_peercred, struct.calcsize("3i"))
        _pid, uid, gid = struct.unpack("3i", creds)
        return PeerIdentity(uid=int(uid), gid=int(gid))
    sol_local = getattr(socket, "SOL_LOCAL", 0)
    local_peercred = getattr(socket, "LOCAL_PEERCRED", 0x001)
    creds = sock.getsockopt(sol_local, local_peercred, struct.calcsize("iI16I"))
    unpacked = struct.unpack("iI16I", creds)
    return PeerIdentity(uid=int(unpacked[1]), gid=int(unpacked[3]))


def _sync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_json_exclusive(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        encoded = json.dumps(dict(payload), separators=(",", ":"), sort_keys=True).encode("utf-8")
        os.write(fd, encoded)
        os.fsync(fd)
    finally:
        os.close(fd)
    _sync_directory(path.parent)


def _ensure_private_directory(root: Path, *, expected_uid: int) -> Path:
    if not root.is_absolute():
        raise ValueError("Source artifact private directory must be an absolute path.")
    for candidate in (root, *root.parents):
        if candidate.is_symlink():
            raise ValueError("Source artifact private directory cannot use symlinks.")
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=False)
    except FileExistsError:
        st = root.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != expected_uid or stat.S_IMODE(st.st_mode) != 0o700:
            raise ValueError("Source artifact private directory must be mode 0700 and owner-held.") from None
    else:
        os.chmod(root, 0o700)
    return root.resolve(strict=True)


def _validate_canonical_uuid4(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("PROTOCOL_REJECTED")
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise ValueError("PROTOCOL_REJECTED") from None
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("PROTOCOL_REJECTED")
    return value


def _validate_namespace(value: Any) -> str:
    if not isinstance(value, str) or _NAMESPACE_RE.fullmatch(value) is None:
        raise ValueError("PROTOCOL_REJECTED")
    return value


def _validate_checksum(value: Any) -> str:
    if not isinstance(value, str) or _CHECKSUM_RE.fullmatch(value) is None:
        raise ValueError("PROTOCOL_REJECTED")
    return value


def _validate_content_type(value: Any, *, allowed: frozenset[str] = DEFAULT_ALLOWED_CONTENT_TYPES) -> str:
    if (
        not isinstance(value, str)
        or not (1 <= len(value) <= 127)
        or _CONTENT_TYPE_RE.fullmatch(value) is None
        or value not in allowed
    ):
        raise ValueError("PROTOCOL_REJECTED")
    return value


def _validate_byte_size(value: Any, *, max_bytes: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("PROTOCOL_REJECTED")
    if value > max_bytes:
        raise ValueError("LIMIT_REJECTED")
    return value


def _validate_object_key(value: Any, *, expected_checksum: str) -> str:
    if not isinstance(value, str):
        raise ValueError("PROTOCOL_REJECTED")
    match = _OBJECT_KEY_RE.fullmatch(value)
    if match is None:
        raise ValueError("PROTOCOL_REJECTED")
    prefix, key_checksum = match.groups()
    if prefix != expected_checksum[:2] or key_checksum != expected_checksum:
        raise ValueError("PROTOCOL_REJECTED")
    return value


class SourceArtifactOwnerServer:
    """Artifact owner 전용 단일 프로세스 Unix domain socket 서버."""

    _active_servers: dict[Path, SourceArtifactOwnerServer] = {}
    _active_lock = threading.Lock()

    def __init__(
        self,
        config: OwnerServerConfig,
        *,
        peer_cred_getter: Callable[[socket.socket], PeerIdentity] | None = None,
        owner_identity: PeerIdentity | None = None,
        drop_reply_after_terminal: bool = False,
    ) -> None:
        _validate_namespace(config.namespace)
        if not config.socket_path.is_absolute():
            raise ValueError("Owner socket path must be absolute.")
        for candidate in (config.socket_path, *config.socket_path.parents):
            if candidate.is_symlink():
                raise ValueError("Owner socket path cannot use symlinks.")

        actual_uid = os.geteuid()
        self._owner_identity = owner_identity or PeerIdentity(uid=actual_uid, gid=os.getegid())
        self._config = config
        self._peer_cred_getter = peer_cred_getter or _get_peer_identity
        self.drop_reply_after_terminal = drop_reply_after_terminal
        self.generation = str(uuid.uuid4())

        _ensure_private_directory(config.artifact_root, expected_uid=actual_uid)
        _ensure_private_directory(config.staging_root, expected_uid=actual_uid)
        _ensure_private_directory(config.outcome_root, expected_uid=actual_uid)
        _ensure_private_directory(config.socket_path.parent, expected_uid=actual_uid)
        self._vault_store = LocalPrivateSourceArtifactStore(config.artifact_root)

        self._manifest_path = config.outcome_root / "owner-manifest.json"
        self._prepare_single_active_owner_manifest()

        if config.socket_path.exists():
            config.socket_path.unlink()

        self._server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server_socket.bind(str(config.socket_path))
        os.chmod(config.socket_path, 0o660)
        self._server_socket.listen(4)
        self._local_wakeup_r, self._local_wakeup_w = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self._local_queue: deque[socket.socket] = deque()
        self._resolved_socket_path = config.socket_path.resolve(strict=True)
        with self._active_lock:
            self._active_servers[self._resolved_socket_path] = self
        self._closed = False

    def open_client_socket(self, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout_seconds)
        try:
            if not os.access(self._config.socket_path, os.W_OK):
                raise PermissionError(errno.EACCES, "Permission denied", str(self._config.socket_path))
            sock.connect(str(self._config.socket_path))
            return sock
        except PermissionError as exc:
            sock.close()
            if exc.errno != errno.EPERM or self._closed:
                raise
            client_end, server_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            client_end.settimeout(timeout_seconds)
            with self._active_lock:
                self._local_queue.append(server_end)
            self._local_wakeup_w.sendall(b"\x01")
            return client_end
        except Exception:
            sock.close()
            raise

    def _prepare_single_active_owner_manifest(self) -> None:
        if self._manifest_path.exists():
            try:
                existing = json.loads(self._manifest_path.read_text(encoding="utf-8"))
                existing_pid = int(existing.get("pid", -1))
            except (OSError, ValueError, json.JSONDecodeError):
                existing_pid = -1
            if existing_pid > 0 and existing_pid != os.getpid():
                try:
                    os.kill(existing_pid, 0)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    raise ValueError("Another SourceArtifactOwnerServer is already active.") from None
                else:
                    if self._config.socket_path.exists():
                        raise ValueError("Another SourceArtifactOwnerServer is already active.")
            self._manifest_path.unlink(missing_ok=True)

        _write_json_exclusive(
            self._manifest_path,
            {
                "schema": OUTCOME_SCHEMA,
                "generation": self.generation,
                "namespace": self._config.namespace,
                "pid": os.getpid(),
                "socket_path": str(self._config.socket_path),
            },
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._active_lock:
            if self._active_servers.get(self._resolved_socket_path) is self:
                self._active_servers.pop(self._resolved_socket_path, None)
            while self._local_queue:
                self._local_queue.popleft().close()
        try:
            self._server_socket.close()
        finally:
            self._local_wakeup_r.close()
            self._local_wakeup_w.close()
            self._config.socket_path.unlink(missing_ok=True)
            self._manifest_path.unlink(missing_ok=True)

    def serve_once(self, *, timeout: float | None = None) -> bool:
        if self._closed:
            return False
        ready, _, _ = select.select([self._server_socket, self._local_wakeup_r], [], [], timeout)
        if not ready:
            return False
        if self._local_wakeup_r in ready:
            try:
                self._local_wakeup_r.recv(1)
            except OSError:
                return False
            with self._active_lock:
                if not self._local_queue:
                    return False
                conn = self._local_queue.popleft()
        else:
            conn, _ = self._server_socket.accept()
        with conn:
            conn.settimeout(DEFAULT_TIMEOUT_SECONDS)
            self._handle_connection(conn)
        return True

    def _handle_connection(self, conn: socket.socket) -> None:
        try:
            peer = self._peer_cred_getter(conn)
        except Exception:
            self._send_safe_rejection(conn, op="UNKNOWN", reason="AUTH_REJECTED")
            return

        if self._config.expected_gid is not None and peer.gid != self._config.expected_gid:
            self._send_safe_rejection(conn, op="UNKNOWN", reason="AUTH_REJECTED")
            return

        try:
            request = read_control_frame(conn)
        except ValueError:
            self._send_safe_rejection(conn, op="UNKNOWN", reason="PROTOCOL_REJECTED")
            return

        op = request.get("op")
        if op not in {"PUT", "READ", "OUTCOME"}:
            self._send_safe_rejection(
                conn,
                op=str(op) if isinstance(op, str) else "UNKNOWN",
                reason="PROTOCOL_REJECTED",
            )
            return

        if op == "PUT":
            self._handle_put(conn, peer=peer, request=request)
        elif op == "READ":
            self._handle_read(conn, peer=peer, request=request)
        else:
            self._handle_outcome(conn, peer=peer, request=request)

    def _send_safe_rejection(
        self,
        conn: socket.socket,
        *,
        op: str,
        reason: str,
        attempt_id: str | None = None,
        result: str = "REJECTED",
    ) -> None:
        payload: dict[str, Any] = {
            "schema": WIRE_SCHEMA,
            "op": op,
            "namespace": self._config.namespace,
            "generation": self.generation,
            "result": result,
            "reason": reason,
        }
        if attempt_id is not None:
            payload["attempt_id"] = attempt_id
        try:
            write_control_frame(conn, payload)
        except OSError:
            return

    def _check_capacity_limits(self, *, namespace: str, byte_size: int) -> bool:
        ns_outcome_dir = self._config.outcome_root / namespace
        if ns_outcome_dir.exists():
            attempt_count = sum(1 for item in ns_outcome_dir.iterdir() if item.is_dir())
            if attempt_count >= self._config.max_journal_attempts:
                return False
        current_staging = sum(item.stat().st_size for item in self._config.staging_root.iterdir() if item.is_file())
        return (current_staging + byte_size) <= self._config.max_staging_bytes

    def _parse_put_request(self, request: Mapping[str, Any]) -> tuple[str, str, str, int, str]:
        expected_keys = {"schema", "op", "namespace", "attempt_id", "checksum", "byte_size", "content_type"}
        if set(request.keys()) != expected_keys:
            raise ValueError("PROTOCOL_REJECTED")
        namespace = _validate_namespace(request["namespace"])
        if namespace != self._config.namespace:
            raise ValueError("PROTOCOL_REJECTED")
        attempt_id = _validate_canonical_uuid4(request["attempt_id"])
        checksum = _validate_checksum(request["checksum"])
        byte_size = _validate_byte_size(request["byte_size"], max_bytes=self._config.max_artifact_bytes)
        content_type = _validate_content_type(request["content_type"], allowed=self._config.allowed_content_types)
        return namespace, attempt_id, checksum, byte_size, content_type

    def _handle_existing_put_attempt(
        self,
        conn: socket.socket,
        *,
        accepted_path: Path,
        terminal_path: Path,
        peer: PeerIdentity,
        namespace: str,
        attempt_id: str,
        checksum: str,
        byte_size: int,
        content_type: str,
    ) -> None:
        try:
            existing_accepted = json.loads(accepted_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._send_safe_rejection(
                conn, op="PUT", attempt_id=attempt_id, result="INDETERMINATE", reason="JOURNAL_INVALID"
            )
            return
        if (
            existing_accepted.get("peer_uid") != peer.uid
            or existing_accepted.get("namespace") != namespace
            or existing_accepted.get("checksum") != checksum
            or existing_accepted.get("byte_size") != byte_size
            or existing_accepted.get("content_type") != content_type
        ):
            self._send_safe_rejection(
                conn, op="PUT", attempt_id=attempt_id, result="REJECTED", reason="ATTEMPT_BINDING_MISMATCH"
            )
            return
        if not terminal_path.exists():
            self._send_safe_rejection(
                conn, op="PUT", attempt_id=attempt_id, result="INDETERMINATE", reason="ATTEMPT_IN_PROGRESS"
            )
            return
        try:
            existing_terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._send_safe_rejection(
                conn, op="PUT", attempt_id=attempt_id, result="INDETERMINATE", reason="JOURNAL_INVALID"
            )
            return
        self._reply_from_terminal_record(conn, op="PUT", attempt_id=attempt_id, record=existing_terminal)

    def _receive_and_store_put_stream(
        self,
        conn: socket.socket,
        *,
        terminal_path: Path,
        accepted_record: Mapping[str, Any],
        namespace: str,
        attempt_id: str,
        checksum: str,
        byte_size: int,
        content_type: str,
    ) -> None:
        staging_path: Path | None = None
        try:
            fd, temp_name = tempfile.mkstemp(prefix=".put-", dir=self._config.staging_root)
            staging_path = Path(temp_name)
            os.fchmod(fd, 0o600)
            digest = hashlib.sha256()
            bytes_read = 0
            with os.fdopen(fd, "wb") as staging_file:
                while True:
                    read_len = min(_CHUNK_SIZE, byte_size - bytes_read + 1)
                    chunk = conn.recv(read_len)
                    if not chunk:
                        break
                    bytes_read += len(chunk)
                    if bytes_read > byte_size:
                        break
                    digest.update(chunk)
                    staging_file.write(chunk)
                staging_file.flush()
                os.fsync(staging_file.fileno())

            if bytes_read != byte_size or digest.hexdigest() != checksum:
                self._record_and_send_terminal(
                    conn,
                    terminal_path=terminal_path,
                    base_record=accepted_record,
                    result="REJECTED",
                    reason="INPUT_MISMATCH",
                    object_key=None,
                )
                return

            metadata = RawArtifactMetadata(
                artifact_key=f"owner/{namespace}/{attempt_id}",
                raw_checksum=checksum,
                byte_size=byte_size,
                content_type=content_type,
            )
            try:
                stored = self._vault_store.put_verified(page_number=1, file_path=staging_path, metadata=metadata)
            except ValueError:
                self._record_and_send_terminal(
                    conn,
                    terminal_path=terminal_path,
                    base_record=accepted_record,
                    result="REJECTED",
                    reason="IMMUTABLE_OBJECT_MISMATCH",
                    object_key=None,
                )
                return

            self._record_and_send_terminal(
                conn,
                terminal_path=terminal_path,
                base_record=accepted_record,
                result="COMPLETE",
                reason=None,
                object_key=stored.object_key,
            )
        except OSError:
            self._record_and_send_terminal(
                conn,
                terminal_path=terminal_path,
                base_record=accepted_record,
                result="INDETERMINATE",
                reason="IO_RESULT_UNKNOWN",
                object_key=None,
            )
        finally:
            if staging_path is not None:
                staging_path.unlink(missing_ok=True)

    def _handle_put(self, conn: socket.socket, *, peer: PeerIdentity, request: Mapping[str, Any]) -> None:
        if peer.uid not in self._config.publisher_uids:
            self._send_safe_rejection(conn, op="PUT", reason="AUTH_REJECTED")
            return
        try:
            namespace, attempt_id, checksum, byte_size, content_type = self._parse_put_request(request)
        except ValueError as exc:
            reason = str(exc) if str(exc) in {"PROTOCOL_REJECTED", "LIMIT_REJECTED"} else "PROTOCOL_REJECTED"
            self._send_safe_rejection(conn, op="PUT", reason=reason)
            return

        attempt_dir = self._config.outcome_root / namespace / attempt_id
        accepted_path = attempt_dir / "accepted.json"
        terminal_path = attempt_dir / "terminal.json"

        if accepted_path.exists():
            self._handle_existing_put_attempt(
                conn,
                accepted_path=accepted_path,
                terminal_path=terminal_path,
                peer=peer,
                namespace=namespace,
                attempt_id=attempt_id,
                checksum=checksum,
                byte_size=byte_size,
                content_type=content_type,
            )
            return

        if not self._check_capacity_limits(namespace=namespace, byte_size=byte_size):
            self._send_safe_rejection(conn, op="PUT", attempt_id=attempt_id, reason="LIMIT_REJECTED")
            return

        expected_key = f"sha256/{checksum[:2]}/{checksum}.artifact"
        accepted_record = {
            "schema": OUTCOME_SCHEMA,
            "event": "ACCEPTED",
            "generation": self.generation,
            "namespace": namespace,
            "attempt_id": attempt_id,
            "peer_uid": peer.uid,
            "peer_gid": peer.gid,
            "storage_backend": _STORAGE_BACKEND,
            "object_key": expected_key,
            "checksum": checksum,
            "byte_size": byte_size,
            "content_type": content_type,
        }
        try:
            _write_json_exclusive(accepted_path, accepted_record)
        except OSError:
            self._send_safe_rejection(
                conn, op="PUT", attempt_id=attempt_id, result="INDETERMINATE", reason="IO_RESULT_UNKNOWN"
            )
            return

        write_control_frame(
            conn,
            {
                "schema": WIRE_SCHEMA,
                "op": "PUT",
                "namespace": namespace,
                "attempt_id": attempt_id,
                "generation": self.generation,
                "result": "ACCEPTED",
                "reason": None,
                "storage_backend": _STORAGE_BACKEND,
                "object_key": expected_key,
                "checksum": checksum,
                "byte_size": byte_size,
                "content_type": content_type,
            },
        )
        self._receive_and_store_put_stream(
            conn,
            terminal_path=terminal_path,
            accepted_record=accepted_record,
            namespace=namespace,
            attempt_id=attempt_id,
            checksum=checksum,
            byte_size=byte_size,
            content_type=content_type,
        )

    def _record_and_send_terminal(
        self,
        conn: socket.socket,
        *,
        terminal_path: Path,
        base_record: Mapping[str, Any],
        result: str,
        reason: str | None,
        object_key: str | None,
    ) -> None:
        terminal_record: dict[str, Any] = {
            **base_record,
            "event": "TERMINAL",
            "result": result,
            "reason": reason,
        }
        if object_key is not None:
            terminal_record["object_key"] = object_key
            terminal_record["storage_backend"] = _STORAGE_BACKEND

        if not terminal_path.exists():
            try:
                _write_json_exclusive(terminal_path, terminal_record)
            except OSError:
                self._send_safe_rejection(
                    conn,
                    op="PUT",
                    attempt_id=str(base_record["attempt_id"]),
                    result="INDETERMINATE",
                    reason="IO_RESULT_UNKNOWN",
                )
                return

        if self.drop_reply_after_terminal:
            return

        self._reply_from_terminal_record(
            conn,
            op="PUT",
            attempt_id=str(base_record["attempt_id"]),
            record=terminal_record,
        )

    def _reply_from_terminal_record(
        self,
        conn: socket.socket,
        *,
        op: str,
        attempt_id: str,
        record: Mapping[str, Any],
    ) -> None:
        reply: dict[str, Any] = {
            "schema": WIRE_SCHEMA,
            "op": op,
            "namespace": str(record.get("namespace", self._config.namespace)),
            "attempt_id": attempt_id,
            "generation": self.generation,
            "result": record["result"],
            "reason": record.get("reason"),
        }
        if record.get("result") == "COMPLETE":
            reply["storage_backend"] = _STORAGE_BACKEND
            reply["object_key"] = record["object_key"]
            reply["checksum"] = record["checksum"]
            reply["byte_size"] = record["byte_size"]
            reply["content_type"] = record["content_type"]
        try:
            write_control_frame(conn, reply)
        except OSError:
            return

    def _handle_read(self, conn: socket.socket, *, peer: PeerIdentity, request: Mapping[str, Any]) -> None:
        if peer.uid not in self._config.publisher_uids and peer.uid not in self._config.reader_uids:
            self._send_safe_rejection(conn, op="READ", reason="AUTH_REJECTED")
            return

        expected_keys = {"schema", "op", "namespace", "object_key", "checksum", "byte_size", "content_type"}
        if set(request.keys()) != expected_keys or conn.recv(1) != b"":
            self._send_safe_rejection(conn, op="READ", reason="PROTOCOL_REJECTED")
            return

        try:
            namespace = _validate_namespace(request["namespace"])
            if namespace != self._config.namespace:
                raise ValueError("PROTOCOL_REJECTED")
            checksum = _validate_checksum(request["checksum"])
            object_key = _validate_object_key(request["object_key"], expected_checksum=checksum)
            byte_size = _validate_byte_size(request["byte_size"], max_bytes=self._config.max_artifact_bytes)
            content_type = _validate_content_type(request["content_type"], allowed=self._config.allowed_content_types)
        except ValueError as exc:
            reason = str(exc) if str(exc) in {"PROTOCOL_REJECTED", "LIMIT_REJECTED"} else "PROTOCOL_REJECTED"
            self._send_safe_rejection(conn, op="READ", reason=reason)
            return

        vault_path = self._config.artifact_root / object_key
        if not vault_path.exists():
            self._send_safe_rejection(conn, op="READ", reason="IMMUTABLE_OBJECT_MISMATCH")
            return

        metadata = RawArtifactMetadata(
            artifact_key=object_key,
            raw_checksum=checksum,
            byte_size=byte_size,
            content_type=content_type,
        )
        try:
            payload_bytes = self._vault_store.read_verified(object_key=object_key, metadata=metadata)
        except ValueError:
            self._send_safe_rejection(conn, op="READ", reason="IMMUTABLE_OBJECT_MISMATCH")
            return

        write_control_frame(
            conn,
            {
                "schema": WIRE_SCHEMA,
                "op": "READ",
                "namespace": namespace,
                "generation": self.generation,
                "result": "COMPLETE",
                "reason": None,
                "storage_backend": _STORAGE_BACKEND,
                "object_key": object_key,
                "checksum": checksum,
                "byte_size": byte_size,
                "content_type": content_type,
            },
        )
        conn.sendall(payload_bytes)

    def _parse_outcome_request(self, request: Mapping[str, Any]) -> tuple[str, str, str] | None:
        if set(request.keys()) != {"schema", "op", "namespace", "attempt_id", "expected_generation"}:
            return None
        try:
            namespace = _validate_namespace(request["namespace"])
            if namespace != self._config.namespace:
                return None
            attempt_id = _validate_canonical_uuid4(request["attempt_id"])
            expected_generation = _validate_canonical_uuid4(request["expected_generation"])
        except ValueError:
            return None
        return namespace, attempt_id, expected_generation

    def _verify_completed_terminal_record(self, terminal_record: Mapping[str, Any]) -> bool:
        if terminal_record.get("result") != "COMPLETE":
            return True
        object_key = str(terminal_record["object_key"])
        metadata = RawArtifactMetadata(
            artifact_key=object_key,
            raw_checksum=str(terminal_record["checksum"]),
            byte_size=int(terminal_record["byte_size"]),
            content_type=str(terminal_record["content_type"]),
        )
        try:
            self._vault_store.read_verified(object_key=object_key, metadata=metadata)
        except ValueError:
            return False
        return True

    def _handle_outcome(self, conn: socket.socket, *, peer: PeerIdentity, request: Mapping[str, Any]) -> None:
        if peer.uid not in self._config.publisher_uids:
            self._send_safe_rejection(conn, op="OUTCOME", reason="AUTH_REJECTED")
            return
        parsed = self._parse_outcome_request(request)
        if parsed is None or conn.recv(1) != b"":
            self._send_safe_rejection(conn, op="OUTCOME", reason="PROTOCOL_REJECTED")
            return
        namespace, attempt_id, expected_generation = parsed

        if expected_generation != self.generation:
            self._send_safe_rejection(
                conn, op="OUTCOME", attempt_id=attempt_id, result="UNKNOWN_GENERATION", reason="GENERATION_UNKNOWN"
            )
            return

        attempt_dir = self._config.outcome_root / namespace / attempt_id
        accepted_path = attempt_dir / "accepted.json"
        terminal_path = attempt_dir / "terminal.json"

        if not accepted_path.exists():
            self._send_safe_rejection(
                conn, op="OUTCOME", attempt_id=attempt_id, result="NOT_OBSERVED", reason="OUTCOME_NOT_OBSERVED"
            )
            return

        try:
            accepted_record = json.loads(accepted_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._send_safe_rejection(
                conn, op="OUTCOME", attempt_id=attempt_id, result="INDETERMINATE", reason="JOURNAL_INVALID"
            )
            return

        if accepted_record.get("peer_uid") != peer.uid or accepted_record.get("namespace") != namespace:
            self._send_safe_rejection(
                conn, op="OUTCOME", attempt_id=attempt_id, result="REJECTED", reason="AUTH_REJECTED"
            )
            return

        if not terminal_path.exists():
            self._send_safe_rejection(
                conn, op="OUTCOME", attempt_id=attempt_id, result="INDETERMINATE", reason="ATTEMPT_IN_PROGRESS"
            )
            return

        try:
            terminal_record = json.loads(terminal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._send_safe_rejection(
                conn, op="OUTCOME", attempt_id=attempt_id, result="INDETERMINATE", reason="JOURNAL_INVALID"
            )
            return

        if not self._verify_completed_terminal_record(terminal_record):
            self._send_safe_rejection(
                conn,
                op="OUTCOME",
                attempt_id=attempt_id,
                result="INDETERMINATE",
                reason="IMMUTABLE_OBJECT_MISMATCH",
            )
            return

        self._reply_from_terminal_record(conn, op="OUTCOME", attempt_id=attempt_id, record=terminal_record)


def _validate_client_socket_file(socket_path: Path) -> None:
    if not socket_path.is_absolute():
        raise ValueError("Source artifact owner socket path must be absolute.")
    for candidate in (socket_path, *socket_path.parents):
        if candidate.is_symlink():
            raise ValueError("Source artifact owner socket path cannot use symlinks.")
    try:
        st = socket_path.lstat()
        parent_st = socket_path.parent.lstat()
    except OSError:
        raise ValueError("Source artifact owner socket could not be inspected.") from None
    if not stat.S_ISSOCK(st.st_mode):
        raise ValueError("Source artifact owner socket path is not a UNIX socket.")
    if (st.st_mode & stat.S_IWOTH) or (parent_st.st_mode & stat.S_IWOTH):
        raise ValueError("Source artifact owner socket and parent must not be world-writable.")
    if not os.access(socket_path, os.W_OK):
        raise PermissionError(errno.EACCES, "Permission denied", str(socket_path))


def _open_raw_client_socket(config: OwnerClientConfig) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(config.timeout_seconds)
    try:
        sock.connect(str(config.socket_path))
        return sock
    except PermissionError as exc:
        sock.close()
        if exc.errno != errno.EPERM:
            raise
        with SourceArtifactOwnerServer._active_lock:
            active_server = SourceArtifactOwnerServer._active_servers.get(config.socket_path.resolve(strict=True))
        if active_server is None:
            raise
        return active_server.open_client_socket(timeout_seconds=config.timeout_seconds)
    except Exception:
        sock.close()
        raise


def _connect_verified_owner_socket(
    config: OwnerClientConfig,
    *,
    peer_cred_getter: Callable[[socket.socket], PeerIdentity],
) -> socket.socket:
    _validate_client_socket_file(config.socket_path)
    sock = _open_raw_client_socket(config)
    try:
        peer = peer_cred_getter(sock)
        if peer.uid != config.expected_owner_uid:
            raise ValueError("Source artifact owner peer UID mismatch.")
        if config.expected_owner_gid is not None and peer.gid != config.expected_owner_gid:
            raise ValueError("Source artifact owner peer GID mismatch.")
    except Exception:
        sock.close()
        raise
    return sock


class OwnerBackedSourceArtifactReader:
    """Owner UDS `READ` 프로토콜을 통해 원본 바이트를 검증 후 수신합니다."""

    def __init__(
        self,
        config: OwnerClientConfig,
        *,
        peer_cred_getter: Callable[[socket.socket], PeerIdentity] | None = None,
    ) -> None:
        _validate_namespace(config.namespace)
        self._config = config
        self._peer_cred_getter = peer_cred_getter or _get_peer_identity

    def read_verified(self, *, object_key: str, metadata: RawArtifactMetadata) -> bytes:
        checksum = _validate_checksum(metadata.raw_checksum)
        _validate_object_key(object_key, expected_checksum=checksum)
        byte_size = _validate_byte_size(metadata.byte_size, max_bytes=self._config.max_artifact_bytes)
        content_type = _validate_content_type(metadata.content_type, allowed=self._config.allowed_content_types)

        sock = _connect_verified_owner_socket(self._config, peer_cred_getter=self._peer_cred_getter)
        with sock:
            write_control_frame(
                sock,
                {
                    "schema": WIRE_SCHEMA,
                    "op": "READ",
                    "namespace": self._config.namespace,
                    "object_key": object_key,
                    "checksum": checksum,
                    "byte_size": byte_size,
                    "content_type": content_type,
                },
            )
            sock.shutdown(socket.SHUT_WR)
            reply = read_control_frame(sock)
            if reply.get("result") != "COMPLETE":
                reason = reply.get("reason") or "PROTOCOL_REJECTED"
                raise ValueError(f"Source artifact owner READ rejected: {reason}")
            if (
                reply.get("namespace") != self._config.namespace
                or reply.get("storage_backend") != _STORAGE_BACKEND
                or reply.get("object_key") != object_key
                or reply.get("checksum") != checksum
                or reply.get("byte_size") != byte_size
                or reply.get("content_type") != content_type
            ):
                raise ValueError("Source artifact owner READ metadata mismatch.")

            received = bytearray()
            digest = hashlib.sha256()
            while True:
                read_len = min(_CHUNK_SIZE, byte_size - len(received) + 1)
                chunk = sock.recv(read_len)
                if not chunk:
                    break
                received.extend(chunk)
                if len(received) > byte_size:
                    raise ValueError("Source artifact owner READ byte size exceeded declared metadata.")
                digest.update(chunk)

        if len(received) != byte_size or digest.hexdigest() != checksum:
            raise ValueError("Source artifact owner READ payload failed verification.")
        return bytes(received)


class OwnerBackedSourceArtifactStore:
    """Owner UDS `PUT` / `OUTCOME` 및 caller-private intent journal을 관리하는 스토어."""

    supports_transaction_cleanup: bool = False

    def __init__(
        self,
        config: OwnerClientConfig,
        *,
        reader: OwnerBackedSourceArtifactReader | None = None,
        peer_cred_getter: Callable[[socket.socket], PeerIdentity] | None = None,
    ) -> None:
        _validate_namespace(config.namespace)
        if config.intent_root is None:
            raise ValueError("OwnerBackedSourceArtifactStore requires an intent_root.")
        _ensure_private_directory(config.intent_root, expected_uid=os.geteuid())
        self._config = config
        self._intent_root = config.intent_root
        self._peer_cred_getter = peer_cred_getter or _get_peer_identity
        self._reader = reader or OwnerBackedSourceArtifactReader(config, peer_cred_getter=self._peer_cred_getter)
        self.finalized: list[StoredRawArtifact] = []
        self.last_attempt_id: str | None = None

    def _verify_accepted_frame(
        self,
        reply: Mapping[str, Any],
        *,
        attempt_id: str,
        expected_key: str,
        checksum: str,
        byte_size: int,
        content_type: str,
    ) -> str:
        accepted_generation = _validate_canonical_uuid4(reply.get("generation"))
        if (
            reply.get("op") != "PUT"
            or reply.get("namespace") != self._config.namespace
            or reply.get("attempt_id") != attempt_id
            or reply.get("storage_backend") != _STORAGE_BACKEND
            or reply.get("object_key") != expected_key
            or reply.get("checksum") != checksum
            or reply.get("byte_size") != byte_size
            or reply.get("content_type") != content_type
        ):
            raise ValueError("PROTOCOL_REJECTED")
        return accepted_generation

    def _verify_complete_terminal_frame(
        self,
        reply: Mapping[str, Any],
        *,
        attempt_id: str,
        accepted_generation: str,
        expected_key: str,
        checksum: str,
        byte_size: int,
        content_type: str,
    ) -> bool:
        return (
            reply.get("op") == "PUT"
            and reply.get("namespace") == self._config.namespace
            and reply.get("attempt_id") == attempt_id
            and reply.get("generation") == accepted_generation
            and reply.get("storage_backend") == _STORAGE_BACKEND
            and reply.get("object_key") == expected_key
            and reply.get("checksum") == checksum
            and reply.get("byte_size") == byte_size
            and reply.get("content_type") == content_type
        )

    def put_verified(
        self,
        *,
        page_number: int | None,
        file_path: Path,
        metadata: RawArtifactMetadata,
        artifact_kind: IngestionArtifactKind = IngestionArtifactKind.RAW_RESPONSE,
        reject_code: str | None = None,
        parser_location: str | None = None,
    ) -> StoredRawArtifact:
        validate_artifact_binding(
            page_number=page_number,
            artifact_kind=artifact_kind,
            reject_code=reject_code,
            parser_location=parser_location,
        )
        verify_raw_artifact(file_path=file_path, metadata=metadata)
        checksum = _validate_checksum(metadata.raw_checksum)
        byte_size = _validate_byte_size(metadata.byte_size, max_bytes=self._config.max_artifact_bytes)
        content_type = _validate_content_type(metadata.content_type, allowed=self._config.allowed_content_types)
        expected_key = f"sha256/{checksum[:2]}/{checksum}.artifact"

        attempt_id = str(uuid.uuid4())
        self.last_attempt_id = attempt_id
        attempt_dir = self._intent_root / self._config.namespace / attempt_id
        prepared_path = attempt_dir / "prepared.json"
        accepted_path = attempt_dir / "accepted.json"
        observed_path = attempt_dir / "observed.json"

        _write_json_exclusive(
            prepared_path,
            {
                "schema": INTENT_SCHEMA,
                "event": "PREPARED",
                "namespace": self._config.namespace,
                "attempt_id": attempt_id,
                "checksum": checksum,
                "byte_size": byte_size,
                "content_type": content_type,
            },
        )

        sock = _connect_verified_owner_socket(self._config, peer_cred_getter=self._peer_cred_getter)
        with sock:
            write_control_frame(
                sock,
                {
                    "schema": WIRE_SCHEMA,
                    "op": "PUT",
                    "namespace": self._config.namespace,
                    "attempt_id": attempt_id,
                    "checksum": checksum,
                    "byte_size": byte_size,
                    "content_type": content_type,
                },
            )
            first_reply = read_control_frame(sock)
            first_result = first_reply.get("result")
            generation = first_reply.get("generation")

            if first_result != "ACCEPTED":
                reason = first_reply.get("reason") or "PROTOCOL_REJECTED"
                _write_json_exclusive(
                    observed_path,
                    {
                        "schema": INTENT_SCHEMA,
                        "event": "OBSERVED",
                        "namespace": self._config.namespace,
                        "attempt_id": attempt_id,
                        "generation": generation,
                        "result": first_result if isinstance(first_result, str) else "REJECTED",
                        "reason": reason,
                        "checksum": checksum,
                        "byte_size": byte_size,
                        "content_type": content_type,
                    },
                )
                raise ValueError(f"Source artifact owner PUT rejected before acceptance: {reason}")

            accepted_generation = self._verify_accepted_frame(
                first_reply,
                attempt_id=attempt_id,
                expected_key=expected_key,
                checksum=checksum,
                byte_size=byte_size,
                content_type=content_type,
            )
            _write_json_exclusive(
                accepted_path,
                {
                    "schema": INTENT_SCHEMA,
                    "event": "ACCEPTED",
                    "namespace": self._config.namespace,
                    "attempt_id": attempt_id,
                    "generation": accepted_generation,
                    "checksum": checksum,
                    "byte_size": byte_size,
                    "content_type": content_type,
                },
            )

            try:
                with file_path.open("rb") as source_file:
                    while True:
                        chunk = source_file.read(_CHUNK_SIZE)
                        if not chunk:
                            break
                        sock.sendall(chunk)
                sock.shutdown(socket.SHUT_WR)
                terminal_reply = read_control_frame(sock)
            except (OSError, ValueError):
                _write_json_exclusive(
                    observed_path,
                    {
                        "schema": INTENT_SCHEMA,
                        "event": "OBSERVED",
                        "namespace": self._config.namespace,
                        "attempt_id": attempt_id,
                        "generation": accepted_generation,
                        "result": "INDETERMINATE",
                        "reason": "CLIENT_TRANSPORT_UNKNOWN",
                        "checksum": checksum,
                        "byte_size": byte_size,
                        "content_type": content_type,
                    },
                )
                raise ValueError("Source artifact owner transport failed: CLIENT_TRANSPORT_UNKNOWN") from None

        term_result = terminal_reply.get("result")
        term_reason = terminal_reply.get("reason")
        term_object_key = terminal_reply.get("object_key")

        _write_json_exclusive(
            observed_path,
            {
                "schema": INTENT_SCHEMA,
                "event": "OBSERVED",
                "namespace": self._config.namespace,
                "attempt_id": attempt_id,
                "generation": accepted_generation,
                "result": term_result if isinstance(term_result, str) else "INDETERMINATE",
                "reason": term_reason if isinstance(term_reason, str) else None,
                "object_key": term_object_key if isinstance(term_object_key, str) else None,
                "checksum": checksum,
                "byte_size": byte_size,
                "content_type": content_type,
            },
        )

        if term_result != "COMPLETE" or not self._verify_complete_terminal_frame(
            terminal_reply,
            attempt_id=attempt_id,
            accepted_generation=accepted_generation,
            expected_key=expected_key,
            checksum=checksum,
            byte_size=byte_size,
            content_type=content_type,
        ):
            raise ValueError(f"Source artifact owner PUT did not complete: {term_reason or term_result}")

        self._reader.read_verified(object_key=expected_key, metadata=metadata)
        stored = StoredRawArtifact(
            page_number=page_number,
            metadata=metadata,
            storage_backend=_STORAGE_BACKEND,
            object_key=expected_key,
            artifact_kind=artifact_kind,
            reject_code=reject_code,
            parser_location=parser_location,
        )
        self.finalized.append(stored)
        return stored

    def get_caller_attempt_state(self, attempt_id: str) -> CallerAttemptState:
        _validate_canonical_uuid4(attempt_id)
        attempt_dir = self._intent_root / self._config.namespace / attempt_id
        prepared_path = attempt_dir / "prepared.json"
        accepted_path = attempt_dir / "accepted.json"
        observed_path = attempt_dir / "observed.json"

        if not prepared_path.exists():
            return CallerAttemptState.NOT_OBSERVED
        if not accepted_path.exists():
            return CallerAttemptState.PREPARED_LOCAL
        if not observed_path.exists():
            return CallerAttemptState.ACCEPTED
        observed = json.loads(observed_path.read_text(encoding="utf-8"))
        result = str(observed.get("result", "INDETERMINATE"))
        return CallerAttemptState(result)

    def query_outcome(self, attempt_id: str) -> OutcomeObservation:
        _validate_canonical_uuid4(attempt_id)
        attempt_dir = self._intent_root / self._config.namespace / attempt_id
        accepted_path = attempt_dir / "accepted.json"
        if not accepted_path.exists():
            raise ValueError("OUTCOME query requires a durable accepted generation in accepted.json.")

        accepted_record = json.loads(accepted_path.read_text(encoding="utf-8"))
        expected_generation = _validate_canonical_uuid4(accepted_record.get("generation"))

        sock = _connect_verified_owner_socket(self._config, peer_cred_getter=self._peer_cred_getter)
        with sock:
            write_control_frame(
                sock,
                {
                    "schema": WIRE_SCHEMA,
                    "op": "OUTCOME",
                    "namespace": self._config.namespace,
                    "attempt_id": attempt_id,
                    "expected_generation": expected_generation,
                },
            )
            sock.shutdown(socket.SHUT_WR)
            reply = read_control_frame(sock)

        result = str(reply.get("result", "REJECTED"))
        reason = reply.get("reason")
        if result == "REJECTED" and reason in {"AUTH_REJECTED", "PROTOCOL_REJECTED"}:
            raise ValueError(f"Source artifact owner OUTCOME rejected: {reason}")

        return OutcomeObservation(
            attempt_id=attempt_id,
            generation=reply.get("generation") if isinstance(reply.get("generation"), str) else None,
            result=result,
            reason=reason if isinstance(reason, str) else None,
            object_key=reply.get("object_key") if isinstance(reply.get("object_key"), str) else None,
            checksum=reply.get("checksum") if isinstance(reply.get("checksum"), str) else None,
            byte_size=reply.get("byte_size") if isinstance(reply.get("byte_size"), int) else None,
            content_type=reply.get("content_type") if isinstance(reply.get("content_type"), str) else None,
        )
