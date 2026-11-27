"""Private local MFDS (data.go.kr 15095677) evaluation corpus builder.

Builds a deterministic, local-only Korean medication label corpus (`EE`, `UD`, `NB`)
from `getDrugPrdtPrmsnDtlInq06` for RAG retrieval/citation evaluation (`CRM-01`).

Safety & governance boundaries:
- `EVAL_CORPUS_LOCAL_ONLY = True`, `RELEASE_ELIGIBLE = False`, `PUBLIC_TRACK_F_ENABLED = False`.
- Output directory is strictly guarded to be outside the git repository root.
- `RAG_MFDS_API_KEY` is read from the environment only during live fetch and is never
  written to manifests, chunk files, logs, or error messages.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import json
import os
import re
import sys
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

import httpx

SectionCode = Literal["EE", "UD", "NB"]

DEFAULT_ENDPOINT_URL = "https://apis.data.go.kr/1471000/DrugPrdtPrmsnInfoService07/getDrugPrdtPrmsnDtlInq06"
EVAL_SOURCE_CODE = "MFDS_DATA_GO_KR_15095677_EVAL"
DEFAULT_OUTPUT_DIR = Path("/tmp/dosey-mfds-eval-corpus")

EVAL_CORPUS_LOCAL_ONLY = True
RELEASE_ELIGIBLE = False
PUBLIC_TRACK_F_ENABLED = False

SCHEMA_VERSION = "mfds-eval-corpus@1"
VALID_SECTIONS: tuple[SectionCode, ...] = ("EE", "UD", "NB")
DEFAULT_MAX_CHUNK_CHARS = 800
DEFAULT_REQUEST_TIMEOUT_SECONDS = 15.0
DEFAULT_SEED_LIMIT = 40

_ITEM_SEQ_RE = re.compile(r"^\d{9}$")
_UNSAFE_XML_DECL_RE = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_PERCENT_ENCODED_RE = re.compile(r"%[0-9A-Fa-f]{2}")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=(?<!\d)\.)\s+|(?<=[!?。！？…])\s+")
_WHITESPACE_RE = re.compile(r"\s+")
_AUTH_ERROR_CODES = frozenset({"20", "30", "31"})
_RATE_LIMIT_CODES = frozenset({"22"})

# Curated 40 representative Korean chronic-disease and common medications across
# 8 therapeutic categories (5 items per category).
DEFAULT_EVAL_SEED_ITEMS: tuple[tuple[str, str], ...] = (
    # hypertension (5)
    ("노바스크정", "hypertension"),
    ("아모잘탄정", "hypertension"),
    ("트윈스타정", "hypertension"),
    ("코자정", "hypertension"),
    ("딜라트렌정", "hypertension"),
    # diabetes (5)
    ("다이아벡스정", "diabetes"),
    ("자누비아정", "diabetes"),
    ("자누메트정", "diabetes"),
    ("자디앙정", "diabetes"),
    ("포시가정", "diabetes"),
    # dyslipidemia (5)
    ("리피토정", "dyslipidemia"),
    ("크레스토정", "dyslipidemia"),
    ("아토젯정", "dyslipidemia"),
    ("로수젯정", "dyslipidemia"),
    ("리바로정", "dyslipidemia"),
    # thyroid (5)
    ("씬지로이드정", "thyroid"),
    ("씬지록신정", "thyroid"),
    ("메티마졸정", "thyroid"),
    ("안티로이드정", "thyroid"),
    ("콤지로이드정", "thyroid"),
    # gi_ppi (5)
    ("넥시움정", "gi_ppi"),
    ("케이캡정", "gi_ppi"),
    ("펙수클루정", "gi_ppi"),
    ("가스터정", "gi_ppi"),
    ("무코스타정", "gi_ppi"),
    # antiplatelet_anticoagulant (5)
    ("플라빅스정", "antiplatelet_anticoagulant"),
    ("아스피린프로텍트정", "antiplatelet_anticoagulant"),
    ("엘리퀴스정", "antiplatelet_anticoagulant"),
    ("릭시아나정", "antiplatelet_anticoagulant"),
    ("자렐토정", "antiplatelet_anticoagulant"),
    # analgesic_nsaid (5)
    ("타이레놀정", "analgesic_nsaid"),
    ("이지엔6애니연질캡슐", "analgesic_nsaid"),
    ("탁센연질캡슐", "analgesic_nsaid"),
    ("쎄레브렉스캡슐", "analgesic_nsaid"),
    ("울트라셋정", "analgesic_nsaid"),
    # allergy_respiratory (5)
    ("지르텍정", "allergy_respiratory"),
    ("알레그라정", "allergy_respiratory"),
    ("싱귤레어정", "allergy_respiratory"),
    ("씨잘정", "allergy_respiratory"),
    ("코대원포르테시럽", "allergy_respiratory"),
)


@dataclass(frozen=True, slots=True)
class EvalDocParagraph:
    """Normalized paragraph chunk extracted from an MFDS DOC XML section."""

    section: SectionCode
    index: int
    locator: str
    section_title: str | None
    article_title: str | None
    text: str


@dataclass(frozen=True, slots=True)
class EvalCorpusChunk:
    """Deterministic evaluation corpus chunk ready for JSONL serialization."""

    chunk_id: str
    item_seq: str
    item_name: str
    section: SectionCode
    locator: str
    source_code: str
    source_version: str
    content: str
    content_sha256: str
    category: str | None = None


@dataclass(frozen=True, slots=True)
class FetchedMfdsItem:
    """Normalized MFDS product permission detail item."""

    item_seq: str
    item_name: str
    query_name: str
    category: str
    source_version_hint: str | None
    section_xml: dict[SectionCode, str]


@dataclass(frozen=True, slots=True)
class BuildCorpusResult:
    """Summary returned after building or dry-running the local evaluation corpus."""

    output_dir: Path
    manifest_path: Path | None
    chunks_path: Path | None
    seed_count: int
    fetched_item_count: int
    skipped_seed_count: int
    chunk_count: int
    dry_run: bool
    manifest_sha256: str | None


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    return _WHITESPACE_RE.sub(" ", normalized).strip()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sanitize_endpoint_url(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def _prepare_xml_text(xml_text: str) -> str:
    if not isinstance(xml_text, str):
        raise ValueError("MFDS DOC XML must be a string.")
    cleaned = xml_text.lstrip("\ufeff").strip()
    if not cleaned:
        raise ValueError("MFDS DOC XML is empty.")
    if cleaned.startswith("<![CDATA[") and cleaned.endswith("]]>"):
        cleaned = cleaned[9:-3].strip()
    if not cleaned:
        raise ValueError("MFDS DOC XML is empty after CDATA unwrap.")
    if cleaned.startswith("&lt;") or ("<" not in cleaned and "&lt;" in cleaned):
        cleaned = html.unescape(cleaned).strip()
    if "\x00" in cleaned or _UNSAFE_XML_DECL_RE.search(cleaned):
        raise ValueError("MFDS DOC XML contains unsafe characters or declarations.")
    return cleaned


def _split_long_text(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]

    parts: list[str] = []
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    if len(sentences) <= 1:
        sentences = _split_by_words_or_hard(text, max_chars)
        return sentences

    current = ""
    for sentence in sentences:
        if len(sentence) > max_chars:
            if current:
                parts.append(current)
                current = ""
            parts.extend(_split_by_words_or_hard(sentence, max_chars))
            continue
        candidate = f"{current} {sentence}".strip() if current else sentence
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                parts.append(current)
            current = sentence
    if current:
        parts.append(current)
    return parts


def _split_by_words_or_hard(text: str, max_chars: int) -> list[str]:
    words = text.split(" ")
    chunks: list[str] = []
    current = ""
    for word in words:
        if not word:
            continue
        if len(word) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            for start in range(0, len(word), max_chars):
                chunks.append(word[start : start + max_chars])
            continue
        candidate = f"{current} {word}".strip() if current else word
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = word
    if current:
        chunks.append(current)
    return chunks


def _walk_doc_node(
    node: ElementTree.Element,
    section_title: str | None,
    article_title: str | None,
    segments: list[tuple[str | None, str | None, str]],
) -> None:
    tag = node.tag.upper()
    if tag == "SECTION":
        next_section = _normalize_text(node.get("title") or "") or section_title
        for child in node:
            _walk_doc_node(child, next_section, article_title, segments)
        return
    if tag == "ARTICLE":
        _walk_article_node(node, section_title, article_title, segments)
        return
    if tag == "PARAGRAPH":
        text = _normalize_text("".join(node.itertext()))
        if text:
            segments.append((section_title, article_title, text))
        return

    direct = _normalize_text(node.text or "")
    if direct and not list(node):
        segments.append((section_title, article_title, direct))
    for child in node:
        _walk_doc_node(child, section_title, article_title, segments)


def _walk_article_node(
    node: ElementTree.Element,
    section_title: str | None,
    article_title: str | None,
    segments: list[tuple[str | None, str | None, str]],
) -> None:
    next_article = _normalize_text(node.get("title") or "") or article_title
    has_structured_children = any(child.tag.upper() in {"SECTION", "ARTICLE", "PARAGRAPH"} for child in node)
    if not has_structured_children:
        text = _normalize_text("".join(node.itertext()))
        if text:
            segments.append((section_title, next_article, text))
        return
    direct_text = _normalize_text(node.text or "")
    if direct_text:
        segments.append((section_title, next_article, direct_text))
    for child in node:
        _walk_doc_node(child, section_title, next_article, segments)


def _extract_raw_segments(
    root: ElementTree.Element,
) -> list[tuple[str | None, str | None, str]]:
    segments: list[tuple[str | None, str | None, str]] = []
    _walk_doc_node(root, _normalize_text(root.get("title") or "") or None, None, segments)
    return segments


def _group_segments_for_chunking(
    raw_segments: Sequence[tuple[str | None, str | None, str]],
    max_chars: int,
) -> list[tuple[str | None, str | None, str]]:
    grouped: list[tuple[str | None, str | None, str]] = []
    for section_title, article_title, text in raw_segments:
        if (
            grouped
            and grouped[-1][0] == section_title
            and grouped[-1][1] == article_title
            and len(grouped[-1][2]) + 1 + len(text) <= max_chars
        ):
            prev_sec, prev_art, prev_text = grouped[-1]
            grouped[-1] = (prev_sec, prev_art, f"{prev_text}\n{text}")
        else:
            grouped.append((section_title, article_title, text))
    return grouped


def parse_mfds_doc_xml(
    xml_text: str,
    *,
    section: SectionCode,
    max_chars: int = DEFAULT_MAX_CHUNK_CHARS,
) -> list[EvalDocParagraph]:
    """Parse MFDS `DOC` XML (`EE_DOC_DATA`/`UD_DOC_DATA`/`NB_DOC_DATA`) into clean chunks.

    Fails closed with `ValueError` on unsupported section, malformed XML, unsafe
    declarations, or documents containing no non-empty plain text.
    """
    if section not in VALID_SECTIONS:
        raise ValueError(f"Unsupported MFDS section: {section!r}")
    if max_chars < 50:
        raise ValueError("max_chars must be at least 50.")

    cleaned = _prepare_xml_text(xml_text)
    try:
        root = ElementTree.fromstring(cleaned)
    except ElementTree.ParseError as exc:
        raise ValueError("Malformed MFDS DOC XML.") from exc

    if root.tag.upper() != "DOC":
        raise ValueError(f"Expected <DOC> root element, got <{root.tag}>.")
    doc_type = (root.get("type") or "").strip().upper()
    if doc_type and doc_type != section:
        raise ValueError(f"DOC type {doc_type!r} does not match expected section {section!r}.")

    raw_segments = _extract_raw_segments(root)
    if not raw_segments:
        raise ValueError(f"MFDS DOC XML for section {section} contains no non-empty text.")

    grouped = _group_segments_for_chunking(raw_segments, max_chars)
    paragraphs: list[EvalDocParagraph] = []
    for section_title, article_title, body_text in grouped:
        prefix = f"{article_title}: " if article_title and not body_text.startswith(article_title) else ""
        available_chars = max(50, max_chars - len(prefix))
        for piece in _split_long_text(body_text, available_chars):
            idx = len(paragraphs)
            full_text = f"{prefix}{piece}" if prefix else piece
            locator = f"{section}:p{idx}" if not article_title else f"{section}:p{idx}:{article_title}"
            paragraphs.append(
                EvalDocParagraph(
                    section=section,
                    index=idx,
                    locator=locator,
                    section_title=section_title,
                    article_title=article_title,
                    text=full_text,
                )
            )

    if not paragraphs:
        raise ValueError(f"MFDS DOC XML for section {section} produced zero paragraphs.")
    return paragraphs


def build_eval_chunks_for_item(
    item: FetchedMfdsItem,
    *,
    max_chars: int = DEFAULT_MAX_CHUNK_CHARS,
) -> list[EvalCorpusChunk]:
    """Convert one fetched MFDS item into deterministic `EvalCorpusChunk` records."""
    chunks: list[EvalCorpusChunk] = []
    for section in VALID_SECTIONS:
        raw_xml = item.section_xml.get(section)
        if not raw_xml or not raw_xml.strip():
            continue
        paragraphs = parse_mfds_doc_xml(raw_xml, section=section, max_chars=max_chars)
        doc_hash = _sha256_text(_normalize_text(raw_xml))
        if item.source_version_hint:
            source_version = f"mfds-15095677:{item.source_version_hint}:{doc_hash[:12]}"
        else:
            source_version = f"mfds-15095677:{doc_hash[:16]}"
        for paragraph in paragraphs:
            chunk_id = f"mfds-15095677:{item.item_seq}:{section}:{paragraph.index}"
            content_sha256 = _sha256_text(paragraph.text)
            chunks.append(
                EvalCorpusChunk(
                    chunk_id=chunk_id,
                    item_seq=item.item_seq,
                    item_name=item.item_name,
                    section=section,
                    locator=paragraph.locator,
                    source_code=EVAL_SOURCE_CODE,
                    source_version=source_version,
                    content=paragraph.text,
                    content_sha256=content_sha256,
                    category=item.category,
                )
            )
    return chunks


def discover_repo_root(start: Path | None = None) -> Path:
    """Locate the enclosing Dosey repository root."""
    anchor = (start or Path(__file__)).resolve()
    for candidate in (anchor, *anchor.parents):
        if (candidate / ".git").exists():
            return candidate
    raise RuntimeError("Could not locate git repository root.")


def validate_output_dir_outside_repo(
    output_dir: Path,
    *,
    repo_root: Path | None = None,
) -> Path:
    """Fail closed if `output_dir` resolves inside the git repository."""
    resolved_repo = (repo_root or discover_repo_root()).resolve()
    resolved_output = output_dir.expanduser().resolve()
    if resolved_output == resolved_repo or resolved_output.is_relative_to(resolved_repo):
        raise ValueError(
            "Output directory must be strictly outside the git repository root so local MFDS payloads are never tracked."
        )
    return resolved_output


def read_mfds_api_key(env: Mapping[str, str] | None = None) -> str:
    """Read `RAG_MFDS_API_KEY` from environment for live fetch; fail closed if missing."""
    source_env = env if env is not None else os.environ
    raw_key = source_env.get("RAG_MFDS_API_KEY", "")
    key = raw_key.strip()
    if not key:
        raise RuntimeError(
            "RAG_MFDS_API_KEY is missing or empty; set it in the environment before running live MFDS fetch."
        )
    if _PERCENT_ENCODED_RE.search(key):
        return unquote(key)
    return key


def _append_unwrapped_item(records: list[dict[str, Any]], entry: object) -> None:
    if not isinstance(entry, dict):
        return
    if set(entry) != {"item"}:
        records.append(entry)
        return
    inner = entry["item"]
    if isinstance(inner, list):
        records.extend(item for item in inner if isinstance(item, dict))
    elif isinstance(inner, dict):
        records.append(inner)


def _extract_dict_items(raw_items: dict[str, Any]) -> list[dict[str, Any]]:
    inner = raw_items.get("item")
    if isinstance(inner, list):
        return [item for item in inner if isinstance(item, dict)]
    if isinstance(inner, dict):
        return [inner]
    if "ITEM_SEQ" in raw_items or "itemSeq" in raw_items:
        return [raw_items]
    return []


def _extract_items_from_json(payload: object) -> tuple[str | None, list[dict[str, Any]]]:
    if not isinstance(payload, dict):
        raise ValueError("MFDS JSON response root must be a JSON object.")
    root = payload.get("response", payload)
    if not isinstance(root, dict):
        raise ValueError("MFDS JSON response envelope must be an object.")
    header = root.get("header")
    result_code = (
        str(header["resultCode"]).strip() if isinstance(header, dict) and header.get("resultCode") is not None else None
    )
    body = root.get("body")
    if not isinstance(body, dict):
        if result_code and result_code != "00":
            return result_code, []
        raise ValueError("MFDS JSON response body is missing.")

    raw_items = body.get("items")
    if raw_items is None or raw_items == "":
        return result_code, []

    if isinstance(raw_items, list):
        records: list[dict[str, Any]] = []
        for entry in raw_items:
            _append_unwrapped_item(records, entry)
        return result_code, records

    if isinstance(raw_items, dict):
        return result_code, _extract_dict_items(raw_items)

    raise ValueError("MFDS JSON body.items has an unexpected type.")


def _extract_xml_field_value(element: ElementTree.Element | None) -> str:
    if element is None:
        return ""
    for child in element:
        if child.tag.upper() == "DOC":
            return ElementTree.tostring(child, encoding="unicode")
    return (element.text or "").strip()


def _extract_items_from_xml_envelope(content: bytes) -> tuple[str | None, list[dict[str, Any]]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("MFDS XML response is not valid UTF-8.") from exc
    if "\x00" in text or _UNSAFE_XML_DECL_RE.search(text):
        raise ValueError("MFDS XML response contains unsafe declarations.")
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise ValueError("Malformed MFDS XML envelope.") from exc

    result_code_el = root.find(".//header/resultCode")
    if result_code_el is None:
        result_code_el = root.find(".//cmmMsgHeader/returnReasonCode")
    result_code = (result_code_el.text or "").strip() if result_code_el is not None else None

    records: list[dict[str, Any]] = []
    for item_el in root.findall(".//body/items/item"):
        record: dict[str, Any] = {}
        for child in item_el:
            tag = child.tag
            if tag.upper() in {"EE_DOC_DATA", "UD_DOC_DATA", "NB_DOC_DATA"}:
                record[tag] = _extract_xml_field_value(child)
            else:
                record[tag] = (child.text or "").strip()
        if record:
            records.append(record)
    return result_code, records


def _decode_response_items(response: httpx.Response) -> list[dict[str, Any]]:
    raw = response.content
    stripped = raw.lstrip()
    content_type = (response.headers.get("content-type") or "").lower()

    if stripped.startswith(b"{") or stripped.startswith(b"[") or "json" in content_type:
        try:
            payload = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("MFDS JSON response could not be decoded.") from exc
        result_code, records = _extract_items_from_json(payload)
    elif stripped.startswith(b"<") or "xml" in content_type:
        result_code, records = _extract_items_from_xml_envelope(raw)
    else:
        raise ValueError("MFDS response format is neither JSON nor XML.")

    if result_code in _AUTH_ERROR_CODES:
        raise RuntimeError(f"MFDS API authentication failed (resultCode={result_code}).")
    if result_code in _RATE_LIMIT_CODES:
        raise RuntimeError(f"MFDS API daily rate limit reached (resultCode={result_code}).")
    if result_code is not None and result_code not in {"00", ""}:
        raise RuntimeError(f"MFDS API returned non-success resultCode={result_code}.")
    return records


def _select_best_record(
    records: Sequence[dict[str, Any]],
    *,
    query_name: str,
) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for rec in records:
        item_seq = str(rec.get("ITEM_SEQ") or rec.get("itemSeq") or "").strip()
        if not item_seq:
            continue
        has_any_doc = any(str(rec.get(field) or "").strip() for field in ("EE_DOC_DATA", "UD_DOC_DATA", "NB_DOC_DATA"))
        if has_any_doc:
            candidates.append(rec)

    if not candidates:
        return None

    normalized_query = _normalize_text(query_name)

    def sort_key(rec: dict[str, Any]) -> tuple[int, int, int, str]:
        name = _normalize_text(str(rec.get("ITEM_NAME") or rec.get("itemName") or ""))
        cancel_name = _normalize_text(str(rec.get("CANCEL_NAME") or ""))
        is_canceled = 1 if cancel_name == "취하" else 0
        is_exact = 0 if name == normalized_query else (1 if name.startswith(normalized_query) else 2)
        item_seq = str(rec.get("ITEM_SEQ") or rec.get("itemSeq") or "")
        return (is_canceled, is_exact, len(name), item_seq)

    return min(candidates, key=sort_key)


def _record_to_fetched_item(
    record: Mapping[str, Any],
    *,
    query_name: str,
    category: str,
) -> FetchedMfdsItem:
    item_seq = str(record.get("ITEM_SEQ") or record.get("itemSeq") or "").strip()
    item_name = _normalize_text(str(record.get("ITEM_NAME") or record.get("itemName") or query_name))
    if not item_seq:
        raise ValueError("Selected MFDS record is missing ITEM_SEQ.")

    change_date = _normalize_text(str(record.get("CHANGE_DATE") or record.get("PERMIT_DATE") or ""))
    section_fields: tuple[tuple[SectionCode, str], ...] = (
        ("EE", "EE_DOC_DATA"),
        ("UD", "UD_DOC_DATA"),
        ("NB", "NB_DOC_DATA"),
    )
    section_xml: dict[SectionCode, str] = {}
    for section, field_name in section_fields:
        raw_val = record.get(field_name)
        if isinstance(raw_val, str) and raw_val.strip():
            section_xml[section] = raw_val

    return FetchedMfdsItem(
        item_seq=item_seq,
        item_name=item_name,
        query_name=query_name,
        category=category,
        source_version_hint=change_date or None,
        section_xml=section_xml,
    )


async def fetch_mfds_item(
    *,
    client: httpx.AsyncClient,
    endpoint_url: str,
    api_key: str,
    query_name: str,
    category: str,
) -> FetchedMfdsItem | None:
    """Fetch one drug product detail record by `item_seq` (9 digits) or `item_name`."""
    params: dict[str, str] = {
        "serviceKey": api_key,
        "type": "json",
        "pageNo": "1",
        "numOfRows": "10",
    }
    stripped_query = query_name.strip()
    if _ITEM_SEQ_RE.fullmatch(stripped_query):
        params["item_seq"] = stripped_query
    else:
        params["item_name"] = stripped_query

    try:
        response = await client.get(endpoint_url, params=params)
    except httpx.HTTPError as exc:
        # Never expose request URL / query params (which include serviceKey).
        raise RuntimeError(f"MFDS HTTP transport error for query {stripped_query!r} ({type(exc).__name__}).") from None

    if response.status_code != 200:
        raise RuntimeError(f"MFDS endpoint returned HTTP {response.status_code} for query {stripped_query!r}.")

    records = _decode_response_items(response)
    best = _select_best_record(records, query_name=stripped_query)
    if best is None:
        return None
    return _record_to_fetched_item(best, query_name=stripped_query, category=category)


def _parse_json_seed_items(stripped: str) -> list[tuple[str, str]]:
    parsed = json.loads(stripped)
    if not isinstance(parsed, list):
        raise ValueError("JSON seed file must contain a list.")
    items: list[tuple[str, str]] = []
    for entry in parsed:
        if isinstance(entry, dict):
            q = str(entry.get("query") or entry.get("drug_query_name") or "").strip()
            cat = str(entry.get("category") or "custom").strip()
        elif isinstance(entry, (list, tuple)) and len(entry) >= 1:
            q = str(entry[0]).strip()
            cat = str(entry[1]).strip() if len(entry) > 1 else "custom"
        else:
            raise ValueError("Invalid JSON seed entry.")
        if not q:
            raise ValueError("Seed query name must be non-empty.")
        items.append((q, cat or "custom"))
    return items


def _parse_text_seed_items(raw: str) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for line in raw.splitlines():
        clean_line = line.strip()
        if not clean_line or clean_line.startswith("#"):
            continue
        delimiter = "\t" if "\t" in clean_line else ("," if "," in clean_line else None)
        if delimiter is not None:
            q, cat = (part.strip() for part in clean_line.split(delimiter, 1))
        else:
            q, cat = clean_line, "custom"
        if q:
            items.append((q, cat or "custom"))
    return items


def load_seed_items(
    seed_file: Path | None = None,
    *,
    limit: int = DEFAULT_SEED_LIMIT,
) -> tuple[tuple[str, str], ...]:
    """Load `(drug_query_name, category)` pairs from `seed_file` or default curated seeds."""
    if limit < 1:
        raise ValueError("--limit must be at least 1.")
    if seed_file is None:
        return DEFAULT_EVAL_SEED_ITEMS[:limit]

    raw = seed_file.read_text(encoding="utf-8")
    stripped = raw.strip()
    if not stripped:
        raise ValueError("Seed file is empty.")

    items = _parse_json_seed_items(stripped) if stripped.startswith("[") else _parse_text_seed_items(raw)
    if not items:
        raise ValueError("No valid seed items found in seed file.")
    return tuple(items[:limit])


async def build_mfds_eval_corpus(
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    limit: int = DEFAULT_SEED_LIMIT,
    endpoint_url: str = DEFAULT_ENDPOINT_URL,
    seed_file: Path | None = None,
    dry_run: bool = False,
    client: httpx.AsyncClient | None = None,
    env: Mapping[str, str] | None = None,
    repo_root: Path | None = None,
) -> BuildCorpusResult:
    """Fetch MFDS drug label sections and write local-only `corpus_chunks.jsonl` + `manifest.json`."""
    safe_output_dir = validate_output_dir_outside_repo(output_dir, repo_root=repo_root)
    seeds = load_seed_items(seed_file, limit=limit)

    if dry_run:
        return BuildCorpusResult(
            output_dir=safe_output_dir,
            manifest_path=None,
            chunks_path=None,
            seed_count=len(seeds),
            fetched_item_count=0,
            skipped_seed_count=0,
            chunk_count=0,
            dry_run=True,
            manifest_sha256=None,
        )

    api_key = read_mfds_api_key(env=env)
    owns_client = client is None
    active_client = (
        httpx.AsyncClient(timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS, follow_redirects=False) if client is None else client
    )

    fetched_items: list[FetchedMfdsItem] = []
    skipped_seeds: list[dict[str, str]] = []
    all_chunks: list[EvalCorpusChunk] = []
    seen_item_seqs: set[str] = set()

    try:
        for query_name, category in seeds:
            fetched = await fetch_mfds_item(
                client=active_client,
                endpoint_url=endpoint_url,
                api_key=api_key,
                query_name=query_name,
                category=category,
            )
            if fetched is None:
                skipped_seeds.append({"query_name": query_name, "category": category, "reason": "NO_MATCH_WITH_DOC"})
                continue
            if fetched.item_seq in seen_item_seqs:
                skipped_seeds.append(
                    {
                        "query_name": query_name,
                        "category": category,
                        "reason": f"DUPLICATE_ITEM_SEQ:{fetched.item_seq}",
                    }
                )
                continue
            try:
                item_chunks = build_eval_chunks_for_item(fetched)
            except ValueError as exc:
                skipped_seeds.append(
                    {
                        "query_name": query_name,
                        "category": category,
                        "reason": f"XML_PARSE_ERROR:{exc}",
                    }
                )
                continue
            if not item_chunks:
                skipped_seeds.append({"query_name": query_name, "category": category, "reason": "NO_TEXT_CHUNKS"})
                continue
            seen_item_seqs.add(fetched.item_seq)
            fetched_items.append(fetched)
            all_chunks.extend(item_chunks)
    finally:
        if owns_client:
            await active_client.aclose()

    safe_output_dir.mkdir(parents=True, exist_ok=True)
    chunks_path = safe_output_dir / "corpus_chunks.jsonl"
    manifest_path = safe_output_dir / "manifest.json"

    jsonl_lines = [json.dumps(asdict(chunk), ensure_ascii=False) for chunk in all_chunks]
    chunks_bytes = ("\n".join(jsonl_lines) + ("\n" if jsonl_lines else "")).encode("utf-8")
    chunks_path.write_bytes(chunks_bytes)
    chunks_sha256 = hashlib.sha256(chunks_bytes).hexdigest()

    section_counts: dict[SectionCode, int] = {"EE": 0, "UD": 0, "NB": 0}
    item_chunk_counts: dict[str, int] = {}
    item_sections: dict[str, list[SectionCode]] = {}
    for chunk in all_chunks:
        section_counts[chunk.section] += 1
        item_chunk_counts[chunk.item_seq] = item_chunk_counts.get(chunk.item_seq, 0) + 1
        sec_list = item_sections.setdefault(chunk.item_seq, [])
        if chunk.section not in sec_list:
            sec_list.append(chunk.section)

    manifest_body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source_code": EVAL_SOURCE_CODE,
        "endpoint_url": _sanitize_endpoint_url(endpoint_url),
        "eval_corpus_local_only": EVAL_CORPUS_LOCAL_ONLY,
        "release_eligible": RELEASE_ELIGIBLE,
        "public_track_f": PUBLIC_TRACK_F_ENABLED,
        "created_at": datetime.now(UTC).isoformat(),
        "limit": limit,
        "seed_count": len(seeds),
        "fetched_item_count": len(fetched_items),
        "skipped_seed_count": len(skipped_seeds),
        "chunk_count": len(all_chunks),
        "section_counts": section_counts,
        "chunks_file": chunks_path.name,
        "chunks_sha256": chunks_sha256,
        "items": [
            {
                "item_seq": item.item_seq,
                "item_name": item.item_name,
                "query_name": item.query_name,
                "category": item.category,
                "chunk_count": item_chunk_counts.get(item.item_seq, 0),
                "sections": item_sections.get(item.item_seq, []),
            }
            for item in fetched_items
        ],
        "skipped_seeds": skipped_seeds,
    }
    canonical_manifest = json.dumps(manifest_body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    manifest_sha256 = hashlib.sha256(canonical_manifest).hexdigest()
    manifest_payload = {**manifest_body, "manifest_sha256": manifest_sha256}
    manifest_path.write_text(
        json.dumps(manifest_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    return BuildCorpusResult(
        output_dir=safe_output_dir,
        manifest_path=manifest_path,
        chunks_path=chunks_path,
        seed_count=len(seeds),
        fetched_item_count=len(fetched_items),
        skipped_seed_count=len(skipped_seeds),
        chunk_count=len(all_chunks),
        dry_run=False,
        manifest_sha256=manifest_sha256,
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a private local MFDS (15095677) RAG evaluation corpus outside the git repository."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory outside the git repository to write corpus files (default: {DEFAULT_OUTPUT_DIR}).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_SEED_LIMIT,
        help=f"Maximum number of seed drug queries to process (default: {DEFAULT_SEED_LIMIT}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate output directory and seed list and print plan without API key or network calls.",
    )
    parser.add_argument(
        "--endpoint-url",
        default=DEFAULT_ENDPOINT_URL,
        help="MFDS getDrugPrdtPrmsnDtlInq06 endpoint URL override.",
    )
    parser.add_argument(
        "--seed-file",
        type=Path,
        default=None,
        help="Optional JSON or TSV/text seed file overriding DEFAULT_EVAL_SEED_ITEMS.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for building the private local MFDS evaluation corpus."""
    args = _build_arg_parser().parse_args(list(argv) if argv is not None else None)
    try:
        result = asyncio.run(
            build_mfds_eval_corpus(
                output_dir=args.output_dir,
                limit=args.limit,
                endpoint_url=args.endpoint_url,
                seed_file=args.seed_file,
                dry_run=args.dry_run,
            )
        )
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if result.dry_run:
        print(
            json.dumps(
                {
                    "mode": "dry_run",
                    "eval_corpus_local_only": EVAL_CORPUS_LOCAL_ONLY,
                    "release_eligible": RELEASE_ELIGIBLE,
                    "public_track_f": PUBLIC_TRACK_F_ENABLED,
                    "planned_seed_count": result.seed_count,
                    "output_dir": str(result.output_dir),
                    "endpoint_url": _sanitize_endpoint_url(args.endpoint_url),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print(
        json.dumps(
            {
                "mode": "live",
                "eval_corpus_local_only": EVAL_CORPUS_LOCAL_ONLY,
                "release_eligible": RELEASE_ELIGIBLE,
                "public_track_f": PUBLIC_TRACK_F_ENABLED,
                "seed_count": result.seed_count,
                "fetched_item_count": result.fetched_item_count,
                "skipped_seed_count": result.skipped_seed_count,
                "chunk_count": result.chunk_count,
                "output_dir": str(result.output_dir),
                "manifest_path": str(result.manifest_path),
                "chunks_path": str(result.chunks_path),
                "manifest_sha256": result.manifest_sha256,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
