#!/usr/bin/env python3
"""Report local document references and temporary source names; never mutate files.

This bounded checker handles inline/reference Markdown links and ATX/Setext headings.
It is not a complete CommonMark renderer or a repository-wide cleanup tool.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

DEFAULT_DOCUMENTS = (
    "AGENTS.md",
    "docs/ai/workflow.md",
    "docs/ai/task-template.md",
    "docs/ai/knowledge-index.md",
    "docs/failures/README.md",
)
SOURCE_ROOTS = ("scripts", "backend/app", "ai_worker", "frontend/src")
EXCLUDED = {
    "runtime",
    "state",
    "cache",
    "__pycache__",
    "data",
    "raw",
    "raw-data",
    "raw_data",
    "outputs",
    "uploads",
    "knowledge",
    "node_modules",
    "dataset",
    "datasets",
    "credentials",
    "secrets",
    "venv",
}
MAX_DOCUMENT_BYTES = 1024 * 1024
INLINE_LINK = re.compile(r"!?\[[^\]\n]*\]\(\s*(<[^>\n]*>|(?:[^\s()\\]|\\.|\([^()]*\))*)(?:\s+[^\n]*?)?\)")
REFERENCE = re.compile(r"^\s{0,3}\[([^\]]+)\]:\s*(<[^>]+>|\S+)")
REFERENCE_LINK = re.compile(r"\[([^\]\n]+)\]\[([^\]\n]*)\]")


def _excluded(name):
    return name.startswith(".") or name.lower() in EXCLUDED


def _safe_path(root, base, name):
    """Check each component before resolving ..; never accept symlink traversal."""
    candidate = Path(name)
    if candidate.is_absolute() or "\\" in name or "\x00" in name:
        raise ValueError("absolute or invalid path")
    current = base
    for part in candidate.parts:
        if part == "..":
            current = current.parent
        else:
            if _excluded(part):
                raise ValueError("excluded path")
            current = current / part
            if current.is_symlink():
                raise ValueError("symlink path")
        if not current.is_relative_to(root):
            raise ValueError("path outside repository")
    return current


def _read_document(path):
    if not path.is_file() or path.suffix.lower() != ".md":
        raise ValueError("document must be a regular Markdown file")
    with path.open("rb") as stream:
        content = stream.read(MAX_DOCUMENT_BYTES + 1)
    if len(content) > MAX_DOCUMENT_BYTES:
        raise ValueError("document exceeds size limit")
    return content.decode("utf-8")


def _lines(text, *, strip_inline_code=True):
    """Preserve line numbering while removing fenced blocks and inline code."""
    fence = None
    for number, line in enumerate(text.splitlines(), 1):
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if fence:
            if re.match(r"^\s{0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*$", line):
                fence = None
            yield number, ""
        elif marker:
            fence = marker[1]
            yield number, ""
        else:
            yield number, re.sub(r"(`+).*?\1", "", line) if strip_inline_code else line


def _anchors(text):
    anchors = set()
    counts = {}
    previous = ""
    for _, line in _lines(text, strip_inline_code=False):
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.+?)(?:\s+#+\s*)?$", line)
        title = heading[1] if heading else previous if re.fullmatch(r"\s{0,3}(?:=+|-+)\s*", line) else None
        if title:
            title = re.sub(r"!?\[([^\]]+)\]\([^)]*\)", r"\1", title)
            slug = re.sub(r"[^\w\-\s]", "", title.lower()).replace(" ", "-")
            slug = re.sub(r"\s", "-", slug)
            count = counts.get(slug, 0)
            counts[slug] = count + 1
            anchors.add(f"{slug}-{count}" if count else slug)
        anchors.update(re.findall(r'<(?:a|[a-z][a-z0-9]*)\b[^>]*\b(?:id|name)=["\']([^"\']+)["\']', line))
        previous = line.strip()
    return anchors


def _links(text):
    lines = list(_lines(text))
    definitions = {}
    for _, line in lines:
        match = REFERENCE.match(line)
        if match:
            definitions[match[1].casefold()] = match[2].strip("<>")
    for number, line in lines:
        definition = REFERENCE.match(line)
        if definition:
            yield number, definition[2].strip("<>")
            continue
        for match in INLINE_LINK.finditer(line):
            yield number, match[1].strip("<>")
        for match in REFERENCE_LINK.finditer(line):
            key = (match[2] or match[1]).casefold()
            if key in definitions:
                yield number, definitions[key]


def _link_problem(root, document, destination):
    parts = urlsplit(destination)
    if parts.scheme.lower() == "file":
        return "unsafe_path", "File URLs are not repository relative references."
    if parts.scheme or parts.netloc:
        return None
    try:
        target = _safe_path(root, document.parent, unquote(parts.path)) if parts.path else document
    except ValueError:
        return "unsafe_path", "Reference uses an excluded, escaping, absolute or symlink path."
    if not target.exists():
        return "missing_target", "Local reference target does not exist."
    if parts.fragment:
        try:
            anchors = _anchors(_read_document(target))
        except (OSError, ValueError, UnicodeError):
            return "uncheckable_anchor", "Anchor target is not a readable bounded Markdown document."
        if unquote(parts.fragment) not in anchors:
            return "missing_anchor", "Local reference anchor does not exist."
    return None


def _source_candidates(root):
    for source in SOURCE_ROOTS:
        try:
            folder = _safe_path(root, root, source)
        except ValueError:
            continue
        if not folder.is_dir():
            continue
        for current, directories, files in os.walk(folder, followlinks=False):
            directories[:] = sorted(n for n in directories if not _excluded(n) and not (Path(current) / n).is_symlink())
            for name in sorted(files):
                path = Path(current) / name
                if path.is_symlink() or _excluded(name):
                    continue
                if name.startswith(("tmp_", "temp_")) or re.search(r"_(?:old|backup)(?:\.|$)", name):
                    yield path.relative_to(root).as_posix()


def scan_hygiene(root, documents=None):
    """Return JSON-serializable report; invalid selection raises ValueError/OSError."""
    root = Path(root).absolute()
    if root.is_symlink() or root.resolve() != root or not root.is_dir():
        raise ValueError("root must be an existing directory without symlink components")
    selected = DEFAULT_DOCUMENTS if documents is None else documents
    checked = []
    findings = []
    for name in dict.fromkeys(selected):
        document = _safe_path(root, root, str(name))
        text = _read_document(document)
        relative = document.relative_to(root).as_posix()
        checked.append(relative)
        for line, destination in _links(text):
            problem = _link_problem(root, document, destination)
            if problem:
                code, message = problem
                findings.append({"path": relative, "line": line, "code": code, "message": message})
    findings.extend(
        {
            "path": path,
            "line": 1,
            "code": "temporary_source",
            "message": "Temporary source filename candidate; review manually, no deletion performed.",
        }
        for path in _source_candidates(root)
    )
    return {"findings": findings, "checked_paths": checked, "mutation": False}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def main(argv=None):
    parser = _Parser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--document", action="append")
    parser.add_argument("--check", action="store_true", help="Report-only check (all invocations are read-only).")
    try:
        args = parser.parse_args(argv)
        report = scan_hygiene(args.root, args.document)
    except (ValueError, OSError, UnicodeError):
        print(
            json.dumps({"error": "Invalid invocation or unreadable document selection.", "code": "invocation_error"}),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(report, ensure_ascii=False))
    return 1 if report["findings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
