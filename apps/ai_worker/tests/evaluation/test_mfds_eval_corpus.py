"""Offline unit tests for the private local MFDS (15095677) evaluation corpus builder."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from ai_worker.tasks.evaluation.mfds_eval_corpus import (
    DEFAULT_ENDPOINT_URL,
    DEFAULT_EVAL_SEED_ITEMS,
    EVAL_SOURCE_CODE,
    BuildCorpusResult,
    EvalCorpusChunk,
    build_mfds_eval_corpus,
    discover_repo_root,
    main,
    parse_mfds_doc_xml,
)


def _sample_doc_xml(section: str, title: str, article_title: str, body_paragraphs: list[str]) -> str:
    paragraphs_xml = "".join(f'<PARAGRAPH tagName="p"><![CDATA[{text}]]></PARAGRAPH>' for text in body_paragraphs)
    return (
        f'<DOC title="{title}" type="{section}">'
        f'<SECTION title="">'
        f'<ARTICLE title="{article_title}">{paragraphs_xml}</ARTICLE>'
        f"</SECTION>"
        f"</DOC>"
    )


def test_default_seed_items_cover_40_drugs_across_8_categories() -> None:
    assert len(DEFAULT_EVAL_SEED_ITEMS) == 40
    categories = {category for _, category in DEFAULT_EVAL_SEED_ITEMS}
    assert categories == {
        "hypertension",
        "diabetes",
        "dyslipidemia",
        "thyroid",
        "gi_ppi",
        "antiplatelet_anticoagulant",
        "analgesic_nsaid",
        "allergy_respiratory",
    }
    assert len({name for name, _ in DEFAULT_EVAL_SEED_ITEMS}) == 40


def test_parse_mfds_doc_xml_handles_sections_cdata_escaped_xml_and_long_splits() -> None:
    ee_xml = _sample_doc_xml(
        "EE",
        "효능효과",
        "1. 고혈압",
        ["본태성 고혈압의 치료에 사용합니다.", "협심증 증상 완화에도 사용합니다."],
    )
    ee_paragraphs = parse_mfds_doc_xml(ee_xml, section="EE")
    assert len(ee_paragraphs) == 1
    assert ee_paragraphs[0].locator == "EE:p0:1. 고혈압"
    assert "1. 고혈압:" in ee_paragraphs[0].text
    assert "본태성 고혈압의 치료에 사용합니다." in ee_paragraphs[0].text

    escaped_ud_xml = (
        "&lt;DOC title=&quot;용법용량&quot; type=&quot;UD&quot;&gt;"
        "&lt;SECTION title=&quot;성인&quot;&gt;"
        "&lt;ARTICLE title=&quot;초기 용량&quot;&gt;"
        "&lt;PARAGRAPH&gt;성인은 1일 1회 5mg을 경구 투여합니다.&lt;/PARAGRAPH&gt;"
        "&lt;/ARTICLE&gt;&lt;/SECTION&gt;&lt;/DOC&gt;"
    )
    ud_paragraphs = parse_mfds_doc_xml(escaped_ud_xml, section="UD")
    assert len(ud_paragraphs) == 1
    assert ud_paragraphs[0].section == "UD"
    assert ud_paragraphs[0].section_title == "성인"
    assert ud_paragraphs[0].article_title == "초기 용량"
    assert ud_paragraphs[0].text == "초기 용량: 성인은 1일 1회 5mg을 경구 투여합니다."

    long_nb_sentence = "심한 간장애 환자에서는 혈중 농도가 상승할 수 있으므로 신중히 투여하십시오. "
    nb_xml = _sample_doc_xml(
        "NB",
        "사용상의주의사항",
        "신중투여",
        [long_nb_sentence * 10],
    )
    nb_paragraphs = parse_mfds_doc_xml(nb_xml, section="NB", max_chars=160)
    assert len(nb_paragraphs) >= 3
    assert all(len(p.text) <= 160 for p in nb_paragraphs)
    assert [p.index for p in nb_paragraphs] == list(range(len(nb_paragraphs)))


@pytest.mark.parametrize(
    "bad_xml",
    [
        "",
        "   ",
        "<DOC><PARAGRAPH>",
        "<NOTDOC><PARAGRAPH>내용</PARAGRAPH></NOTDOC>",
        '<DOC title="효능효과" type="UD"><PARAGRAPH>섹션 불일치</PARAGRAPH></DOC>',
        '<DOC title="효능효과" type="EE"><SECTION title=""><ARTICLE title="빈 항목"><PARAGRAPH>   </PARAGRAPH></ARTICLE></SECTION></DOC>',
        '<!DOCTYPE foo><DOC title="효능효과" type="EE"><PARAGRAPH>금지</PARAGRAPH></DOC>',
    ],
)
def test_parse_mfds_doc_xml_rejects_malformed_or_empty_fail_closed(bad_xml: str) -> None:
    with pytest.raises(ValueError):
        parse_mfds_doc_xml(bad_xml, section="EE")


def test_build_corpus_offline_json_and_xml_envelopes_deterministic_chunks(tmp_path: Path) -> None:
    observed_queries: list[dict[str, str]] = []
    secret_value = "test-secret-api-key-DO-NOT-LEAK-98765"

    ee_xml = _sample_doc_xml("EE", "효능효과", "적응증", ["고혈압 치료에 사용합니다."])
    ud_xml = _sample_doc_xml("UD", "용법용량", "성인", ["1일 1회 5mg 경구 투여합니다."])
    nb_xml = _sample_doc_xml("NB", "사용상의주의사항", "금기", ["중증 저혈압 환자에게 투여하지 마십시오."])

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        observed_queries.append(params)
        if params.get("item_name") == "노바스크정":
            return httpx.Response(
                200,
                json={
                    "header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE."},
                    "body": {
                        "pageNo": 1,
                        "numOfRows": 10,
                        "totalCount": 1,
                        "items": [
                            {
                                "ITEM_SEQ": "199701417",
                                "ITEM_NAME": "노바스크정5밀리그램",
                                "CHANGE_DATE": "20250115",
                                "EE_DOC_DATA": ee_xml,
                                "UD_DOC_DATA": ud_xml,
                                "NB_DOC_DATA": nb_xml,
                            }
                        ],
                    },
                },
            )
        if params.get("item_seq") == "200001234":
            escaped_ee = ee_xml.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            plain_nb = '<DOC title="사용상의주의사항" type="NB"><PARAGRAPH>중증 저혈압 환자에게 투여하지 마십시오.</PARAGRAPH></DOC>'
            xml_body = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                "<response>"
                "<header><resultCode>00</resultCode></header>"
                "<body><items><item>"
                "<ITEM_SEQ>200001234</ITEM_SEQ>"
                "<ITEM_NAME>합성테스트정</ITEM_NAME>"
                "<CHANGE_DATE>20250201</CHANGE_DATE>"
                f"<EE_DOC_DATA>{escaped_ee}</EE_DOC_DATA>"
                f"<UD_DOC_DATA>{ud_xml}</UD_DOC_DATA>"
                f"<NB_DOC_DATA><![CDATA[{plain_nb}]]></NB_DOC_DATA>"
                "</item></items></body>"
                "</response>"
            )
            return httpx.Response(200, content=xml_body.encode("utf-8"), headers={"content-type": "application/xml"})
        return httpx.Response(
            200,
            json={"header": {"resultCode": "00"}, "body": {"items": None}},
        )

    seed_file = tmp_path / "seeds.json"
    seed_file.write_text(
        json.dumps(
            [
                {"query": "노바스크정", "category": "hypertension"},
                {"query": "200001234", "category": "diabetes"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    out_dir = tmp_path / "corpus_out"

    async def run() -> BuildCorpusResult:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await build_mfds_eval_corpus(
                output_dir=out_dir,
                limit=2,
                endpoint_url=DEFAULT_ENDPOINT_URL,
                seed_file=seed_file,
                client=client,
                env={"RAG_MFDS_API_KEY": secret_value},
            )

    result = asyncio.run(run())
    assert result.fetched_item_count == 2
    assert result.chunk_count == 6
    assert observed_queries[0]["item_name"] == "노바스크정"
    assert observed_queries[1]["item_seq"] == "200001234"
    assert all(q["serviceKey"] == secret_value for q in observed_queries)

    chunks_path = out_dir / "corpus_chunks.jsonl"
    manifest_path = out_dir / "manifest.json"
    assert chunks_path.is_file()
    assert manifest_path.is_file()

    chunk_rows = [json.loads(line) for line in chunks_path.read_text(encoding="utf-8").splitlines()]
    assert len(chunk_rows) == 6
    first = EvalCorpusChunk(**chunk_rows[0])
    assert first.chunk_id == "mfds-15095677:199701417:EE:0"
    assert first.source_code == EVAL_SOURCE_CODE
    assert first.content_sha256 == hashlib.sha256(first.content.encode("utf-8")).hexdigest()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["eval_corpus_local_only"] is True
    assert manifest["release_eligible"] is False
    assert manifest["public_track_f"] is False
    assert manifest["seed_count"] == 2
    assert manifest["fetched_item_count"] == 2
    assert manifest["chunk_count"] == 6
    assert manifest["section_counts"] == {"EE": 2, "UD": 2, "NB": 2}
    assert len(manifest["manifest_sha256"]) == 64

    # Secret non-leakage check across serialized outputs.
    assert secret_value not in chunks_path.read_text(encoding="utf-8")
    assert secret_value not in manifest_path.read_text(encoding="utf-8")


def test_output_dir_inside_repo_rejected_fail_closed() -> None:
    repo_root = discover_repo_root()
    inside_path = repo_root / "tmp_forbidden_eval_corpus"

    with pytest.raises(ValueError, match="outside the git repository"):
        asyncio.run(build_mfds_eval_corpus(output_dir=inside_path, dry_run=True))

    exit_code = main(["--dry-run", "--output-dir", str(inside_path)])
    assert exit_code == 1


def test_missing_api_key_fails_closed_without_http_calls(tmp_path: Path) -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await build_mfds_eval_corpus(
                output_dir=tmp_path / "out",
                limit=1,
                client=client,
                env={},
            )

    with pytest.raises(RuntimeError, match="RAG_MFDS_API_KEY"):
        asyncio.run(run())
    assert called is False


def test_secret_never_leaks_on_transport_error_or_cli_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret_value = "super-sensitive-mfds-key-XYZ-777"

    def failing_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"boom while calling {request.url}", request=request)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(failing_handler)) as client:
            await build_mfds_eval_corpus(
                output_dir=tmp_path / "out",
                limit=1,
                client=client,
                env={"RAG_MFDS_API_KEY": secret_value},
            )

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(run())
    assert secret_value not in str(exc_info.value)

    monkeypatch.setenv("RAG_MFDS_API_KEY", secret_value)
    exit_code = main(["--dry-run", "--output-dir", str(tmp_path / "dry_out")])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert secret_value not in captured.out
    assert secret_value not in captured.err


def test_cli_dry_run_succeeds_without_api_key_or_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("RAG_MFDS_API_KEY", raising=False)
    out_dir = tmp_path / "dry_run_corpus"

    exit_code = main(["--dry-run", "--output-dir", str(out_dir), "--limit", "40"])
    assert exit_code == 0

    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert summary["mode"] == "dry_run"
    assert summary["eval_corpus_local_only"] is True
    assert summary["release_eligible"] is False
    assert summary["public_track_f"] is False
    assert summary["planned_seed_count"] == 40
    assert Path(summary["output_dir"]) == out_dir.resolve()
    assert not out_dir.exists()
