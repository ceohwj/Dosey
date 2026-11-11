"""Local cooperative writer admission and canonical task recovery.

A claim coordinates cooperating callers; it grants no filesystem permissions and
is not confinement. Only the controller's sandbox and assignment grant authority.
"""

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

MAX_JSON = 1_048_576
MAX_CONTEXT = 4_194_304
STORE = ".omx/plans/dosey-agent-tasks"
ID = re.compile(r"[a-z0-9-]{1,64}\Z")
LIST_FIELDS = (
    "allowed_files",
    "forbidden_files",
    "dependencies",
    "authoritative_contract",
    "acceptance_criteria",
    "required_tests",
    "non_goals",
    "completed",
    "known_gaps",
    "knowledge_refs",
)
BLOCKED_PARTS = {
    ".git",
    ".codex",
    ".aws",
    ".ssh",
    ".omx",
    "data",
    "raw",
    "raw-data",
    "raw_data",
    "uploads",
    "outputs",
    "knowledge",
    "credentials",
    "secrets",
    "node_modules",
    "__pycache__",
}


class WorkflowError(Exception):
    def __init__(self, message, code="invalid"):
        super().__init__(message)
        self.code = code


def _integer(value):
    return type(value) is int and value >= 0


def _number(value):
    return type(value) is int and value >= 0 or type(value) is float and math.isfinite(value) and value >= 0


def _task_id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise WorkflowError("Invalid task ID")
    return value


def _relative(value, markdown=False):
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise WorkflowError("Invalid repository path")
    parts = value.split("/")
    if any(p in ("", ".", "..") for p in parts) or any(c in value for c in "\\*?[]\x00\n\r:"):
        raise WorkflowError("Repository paths must be exact relative paths")
    if any(p.lower() in BLOCKED_PARTS or ".env" in p.lower() or p.lower().endswith((".pem", ".key")) for p in parts):
        raise WorkflowError("Sensitive or runtime context path excluded")
    if markdown and Path(value).suffix.lower() != ".md":
        raise WorkflowError("Document references must be Markdown paths")
    return value


def _inspect_json(item, depth=0):
    if depth > 24:
        raise WorkflowError("JSON nesting limit exceeded")
    if item is None or type(item) in (str, bool, int):
        return
    if type(item) is float and math.isfinite(item):
        return
    if type(item) is dict and all(type(key) is str for key in item):
        children, limit = item.values(), 256
    elif type(item) is list:
        children, limit = item, 1024
    else:
        raise WorkflowError("Invalid JSON value")
    if len(item) > limit:
        raise WorkflowError("JSON container limit exceeded")
    for child in children:
        _inspect_json(child, depth + 1)


def _json_bytes(value):
    _inspect_json(value)
    try:
        data = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise WorkflowError("Invalid JSON encoding") from exc
    if len(data) > MAX_JSON:
        raise WorkflowError("JSON size limit exceeded")
    return data


def validate_record(record):
    _json_bytes(record)
    if type(record) is not dict:
        raise WorkflowError("Task record must be an object")
    required = set(LIST_FIELDS) | {
        "schema_version",
        "task_id",
        "goal",
        "base_revision",
        "phase",
        "revision",
        "next_action",
        "budget",
        "operation_in_flight",
    }
    if not required <= record.keys():
        raise WorkflowError("Missing task record fields")
    if type(record["schema_version"]) is not int or record["schema_version"] != 1:
        raise WorkflowError("Unsupported schema version")
    _task_id(record["task_id"])
    if not _integer(record["revision"]):
        raise WorkflowError("Invalid revision")
    if record["phase"] not in ("planned", "running", "review", "complete", "blocked"):
        raise WorkflowError("Invalid phase")
    _validate_text_and_lists(record)
    _validate_budget(record["budget"])


def _validate_text_and_lists(record):
    for field in ("goal", "base_revision", "next_action"):
        if type(record[field]) is not str:
            raise WorkflowError("Invalid text field")
    if type(record["operation_in_flight"]) is not bool:
        raise WorkflowError("Invalid operation flag")
    for field in LIST_FIELDS:
        value = record[field]
        if type(value) is not list or any(type(item) is not str for item in value):
            raise WorkflowError("Invalid string list")
    for field in ("allowed_files", "forbidden_files", "authoritative_contract", "knowledge_refs"):
        for value in record[field]:
            _relative(value, markdown=field in ("authoritative_contract", "knowledge_refs"))


def _validate_budget(budget):
    if type(budget) is not dict or set(budget) != {"max_cycles", "max_active_seconds", "cycles_used", "active_seconds"}:
        raise WorkflowError("Invalid budget fields")
    for field, predicate in (
        ("max_cycles", _integer),
        ("max_active_seconds", _number),
        ("cycles_used", _integer),
        ("active_seconds", _number),
    ):
        value = budget[field]
        if value is None and field in ("cycles_used", "active_seconds"):
            continue
        if not predicate(value):
            raise WorkflowError("Invalid budget counter")


def _safe_components(path):
    path = Path(os.path.abspath(path))
    for component in reversed((path, *path.parents)):
        try:
            mode = component.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise WorkflowError("Symlink components are not allowed")
    return path


def _root(root):
    path = _safe_components(root)
    if not path.is_dir():
        raise WorkflowError("Repository root must be an existing directory")
    return path


def _store(root):
    return _safe_components(_root(root) / STORE)


def _read_bytes(path, limit=MAX_JSON):
    path = _safe_components(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise WorkflowError("Expected a bounded regular file")
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise WorkflowError("File size limit exceeded")
    return value


def _read_json(path):
    try:
        value = json.loads(_read_bytes(path))
        _json_bytes(value)
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise WorkflowError("Malformed JSON") from exc


def _canonical(root, task_id):
    path = _store(root) / _task_id(task_id) / "task.json"
    item = _read_json(path)
    validate_record(item)
    if item["task_id"] != task_id:
        raise WorkflowError("Task ID does not match canonical location")
    _context_paths(root, item)
    return item


def _context_paths(root, record):
    root = _root(root)
    paths = set()
    for field in ("allowed_files", "forbidden_files", "authoritative_contract", "knowledge_refs"):
        for value in record[field]:
            _safe_components(root / value)
            if field != "forbidden_files":
                paths.add(value)
    return sorted(paths)


def _head(root):
    try:
        result = subprocess.run(
            ["git", "-C", str(_root(root)), "rev-parse", "--verify", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return value.decode("ascii") if result.returncode == 0 and re.fullmatch(rb"[0-9a-f]{40,64}", value) else None


def _snapshot(root, record):
    hashes = {}
    for relative in _context_paths(root, record):
        try:
            data = _read_bytes(_root(root) / relative, MAX_CONTEXT)
        except FileNotFoundError:
            hashes[relative] = "MISSING"
        else:
            hashes[relative] = hashlib.sha256(data).hexdigest()
    return {
        "canonical_digest": hashlib.sha256(_json_bytes(record)).hexdigest(),
        "revision": record["revision"],
        "head": _head(root),
        "paths": hashes,
    }


def _reconciliation(record):
    budget = record["budget"]
    return record["operation_in_flight"] or any(
        budget[field] is None or budget[field] >= budget[maximum]
        for field, maximum in (("cycles_used", "max_cycles"), ("active_seconds", "max_active_seconds"))
    )


def discover_tasks(root):
    store = _store(root)
    if not store.exists():
        return []
    summaries = []
    for path in sorted(store.iterdir()):
        if path.name.startswith("."):
            continue
        _task_id(path.name)
        _safe_components(path)
        if not path.is_dir():
            continue
        if not _safe_components(path / "task.json").exists():
            continue
        record = _canonical(root, path.name)
        summaries.append({key: record[key] for key in ("task_id", "goal", "phase", "next_action", "revision")})
    return summaries


def restore_task(root, task_id):
    record = _canonical(root, task_id)
    status, reason = "UNKNOWN", "Snapshot cache missing or malformed"
    cache_path = _safe_components(_store(root) / task_id / "checkpoint.json")
    try:
        cache = _read_json(cache_path)
        valid = type(cache) is dict and set(cache) == {"canonical_digest", "revision", "head", "paths"}
        valid = (
            valid
            and _integer(cache["revision"])
            and type(cache["canonical_digest"]) is str
            and bool(re.fullmatch("[0-9a-f]{64}", cache["canonical_digest"]))
        )
        valid = valid and (
            cache["head"] is None or type(cache["head"]) is str and bool(re.fullmatch("[0-9a-f]{40,64}", cache["head"]))
        )
        valid = (
            valid
            and type(cache["paths"]) is dict
            and all(
                type(key) is str and type(value) is str and (value == "MISSING" or re.fullmatch("[0-9a-f]{64}", value))
                for key, value in cache["paths"].items()
            )
        )
        if valid:
            matched = cache == _snapshot(root, record)
            status = "MATCHED_SNAPSHOT" if matched else "STALE"
            reason = "Snapshot matches current files" if matched else "Canonical record, files or Git HEAD changed"
    except (WorkflowError, OSError):
        pass
    reconciliation = _reconciliation(record)
    if reconciliation:
        status, reason = "UNKNOWN", "Budget or operation requires evidence reconciliation"
    return {"task": record, "verification": status, "reconciliation_required": reconciliation, "reason": reason}


@contextmanager
def _metadata_lock(root):
    store = _store(root)
    store.mkdir(parents=True, exist_ok=True)
    path = _safe_components(store / ".metadata.lock")
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise WorkflowError("Invalid metadata lock")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield store
    finally:
        os.close(descriptor)


def _check_claim(store, task_id, claim):
    try:
        holder = _read_json(store / ".writer-claim.json")
    except FileNotFoundError as exc:
        raise WorkflowError("No writer claim exists", "conflict") from exc
    if (
        type(holder) is not dict
        or holder.get("task_id") != task_id
        or type(claim) is not str
        or not claim
        or holder.get("claim") != claim
    ):
        raise WorkflowError("Writer claim does not match holder", "conflict")


def claim_writer(root, task_id):
    _task_id(task_id)
    with _metadata_lock(root) as store:
        path = _safe_components(store / ".writer-claim.json")
        if path.exists():
            raise WorkflowError("A cooperating writer is already admitted", "conflict")
        canonical = _safe_components(store / task_id / "task.json")
        if canonical.exists() and _reconciliation(_canonical(root, task_id)):
            raise WorkflowError("Budget or operation requires evidence reconciliation", "reconciliation_required")
        result = {"task_id": task_id, "claim": secrets.token_hex(32)}
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError as exc:
            raise WorkflowError("A cooperating writer is already admitted", "conflict") from exc
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(_json_bytes(result))
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            path.unlink()
            raise
        return result


def _atomic_json(path, value):
    _safe_components(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def checkpoint_task(root, task_id, claim, expected_revision, record):
    _task_id(task_id)
    validate_record(record)
    if record["task_id"] != task_id or not _integer(expected_revision):
        raise WorkflowError("Invalid task ID or expected revision")
    with _metadata_lock(root) as store:
        _check_claim(store, task_id, claim)
        folder = _safe_components(store / task_id)
        canonical = _safe_components(folder / "task.json")
        previous = _canonical(root, task_id) if canonical.exists() else None
        actual = previous["revision"] if previous else 0
        if expected_revision != actual:
            raise WorkflowError("Canonical revision changed", "conflict")
        if previous:
            for field in ("cycles_used", "active_seconds"):
                before, after = previous["budget"][field], record["budget"][field]
                if after is not None and (before is None or after < before):
                    raise WorkflowError("Used budget cannot be silently reduced or reset", "reconciliation_required")
        saved = dict(record, revision=actual + 1)
        snapshot = _snapshot(root, saved)
        folder.mkdir(exist_ok=True)
        try:
            _atomic_json(canonical, saved)
            _atomic_json(folder / "checkpoint.json", snapshot)
        except OSError as exc:
            raise WorkflowError("Checkpoint I/O failed; restore canonical state before retry", "io_error") from exc
        return {"task_id": task_id, "revision": saved["revision"], "verification": "MATCHED_SNAPSHOT"}


def release_writer(root, task_id, claim):
    _task_id(task_id)
    with _metadata_lock(root) as store:
        _check_claim(store, task_id, claim)
        (store / ".writer-claim.json").unlink()
    return {"task_id": task_id, "released": True}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise WorkflowError(message, "usage")


def _parser():
    parser = _Parser(description=__doc__)
    parser.add_argument("--root", default=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    for name in ("discover", "restore", "claim", "checkpoint", "validate", "release"):
        command = commands.add_parser(name)
        command.add_argument("--root", default=argparse.SUPPRESS)
        if name != "discover":
            command.add_argument("--task", required=True)
        if name in ("checkpoint", "release"):
            command.add_argument("--claim", required=True)
        if name == "checkpoint":
            command.add_argument("--expected-revision", required=True, type=int)
            command.add_argument("--record", required=True)
    return parser


def _read_record_input(path, root):
    path = _safe_components(path)
    # Validate before opening even when the input is outside the repository.
    repository = _root(root)
    parts = path.relative_to(repository).parts if path.is_relative_to(repository) else path.parts[1:]
    for part in parts:
        _relative(part)
    return _read_json(path)


def _dispatch(args):
    root = getattr(args, "root", ".")
    if args.command == "discover":
        result = {"tasks": discover_tasks(root)}
    elif args.command == "restore":
        result = restore_task(root, args.task)
    elif args.command == "claim":
        result = claim_writer(root, args.task)
    elif args.command == "checkpoint":
        result = checkpoint_task(
            root, args.task, args.claim, args.expected_revision, _read_record_input(args.record, root)
        )
    elif args.command == "release":
        result = release_writer(root, args.task, args.claim)
    else:
        restored = restore_task(root, args.task)
        result = {
            "task_id": args.task,
            "valid": True,
            "verification": restored["verification"],
            "reconciliation_required": restored["reconciliation_required"],
        }
    return result


def main(argv=None):
    try:
        args = _parser().parse_args(argv)
        result = _dispatch(args)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (WorkflowError, OSError) as exc:
        print(
            json.dumps(
                {
                    "error": str(exc) if isinstance(exc, WorkflowError) else "Filesystem operation failed",
                    "code": exc.code if isinstance(exc, WorkflowError) else "io_error",
                }
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
