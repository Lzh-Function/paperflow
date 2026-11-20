import base64
import hashlib
import json
import random
import tempfile
from dataclasses import replace
from pathlib import Path

import pymupdf
import pytest

from paperflow.agents.base import AnalysisRequest, AnalysisResult, PDFDocument, ProviderError
from paperflow.agents.claude_cli import ClaudeCliProvider
from paperflow.agents.codex_cli import CodexCliProvider
from paperflow.agents.document import claude_pdf_parts, page_images, prepare_claude_pdf, snapshot
from paperflow.agents.fake import FakeProvider
from paperflow.agents.schema import VISUAL_SCHEMA, make_prompt, validate, validate_response
from paperflow.pipeline import Pipeline
from paperflow.resources.wiki_extract_visuals import extract_visuals
from paperflow.store import Store


def visual_data(request):
    data = FakeProvider().analyze(replace(request, document=None)).data
    data["key_findings_ja"] = ["合成試験の所見です。"]
    data["visual_review"] = dict(
        status="completed",
        reason_ja="",
        pages_viewed=list(range(1, request.document.page_count + 1)),
        selected_visuals=[
            dict(
                page=request.document.page_count,
                kind="figure",
                number=2,
                title_ja="合成図の説明",
                explanation_ja="図から読み取った合成試験用の説明です。",
                finding_indices=[1],
                uncertainty_ja="人間による照合は未実施です。",
            )
        ],
    )
    return data


@pytest.fixture
def visual_request(tmp_path):
    pdf = tmp_path / "private-source.pdf"
    with pymupdf.open() as doc:
        for i in range(2):
            p = doc.new_page(width=400, height=400)
            p.draw_rect(pymupdf.Rect(50, 80, 280, 220), color=(1, 0, 0), fill=(1, 0, 0))
            p.insert_text((50, 235), f"Fig. {i + 1} Synthetic vector")
        doc.save(pdf)
    return AnalysisRequest(
        "synthetic",
        {"title": "Synthetic"},
        "合成試験本文",
        "fulltext",
        [1, 2],
        ["other"],
        document=PDFDocument(pdf, hashlib.sha256(pdf.read_bytes()).hexdigest(), 2),
    )


def test_visual_prompt_does_not_leak_local_path(visual_request):
    text = make_prompt(visual_request)
    assert "private-source.pdf" not in text and str(visual_request.document.path) not in text
    assert "添付PDF・画像" in text and "視覚読解の追加要件" in text


def test_native_pdf_prompt_omits_duplicate_text_without_losing_context(visual_request):
    original = visual_request.text
    visual_request.research_context = "専門と広い関心を保持する。"
    visual_request.text = "本文だけの固有文字列" * 10000
    text = make_prompt(visual_request, text_in_pdf=True)
    payload = json.loads(text.split("非信頼の資料JSON:\n", 1)[1])
    assert "本文だけの固有文字列" not in text
    assert payload["text_source"] == "attached_pdf"
    assert payload["pages_available"] == [1, 2]
    assert payload["research_context"] == visual_request.research_context
    assert payload["coverage"] == visual_request.coverage
    assert payload["visual_input"]["page_count"] == 2
    assert visual_request.text == "本文だけの固有文字列" * 10000
    # Codexのページ画像経路では補助本文を引き続き渡す。
    visual_request.text = original
    assert original in make_prompt(visual_request)
    with pytest.raises(ValueError):
        make_prompt(replace(visual_request, document=None), text_in_pdf=True)


@pytest.mark.parametrize(
    "problem", ["missing-page", "unable", "bad-selection", "bad-finding", "no-visual-review"]
)
def test_visual_completion_cannot_silently_be_text_only(visual_request, problem):
    data = visual_data(visual_request)
    if problem == "missing-page":
        data["visual_review"]["pages_viewed"] = [1]
    elif problem == "unable":
        data["visual_review"]["status"] = "unable"
    elif problem == "bad-selection":
        data["visual_review"]["selected_visuals"][0]["page"] = 3
    elif problem == "bad-finding":
        data["visual_review"]["selected_visuals"][0]["finding_indices"] = [2]
    else:
        del data["visual_review"]
    with pytest.raises(ProviderError):
        validate(data, visual_request)


def test_cached_visual_analysis_can_be_validated_for_export(visual_request):
    data = visual_data(visual_request)
    assert validate(data, replace(visual_request, document=None)) == data


@pytest.mark.parametrize("label", ["4.4", "4.10", "A.1", "S2", "IV", "2(a)", 2, None])
def test_compound_visual_numbers_are_valid(visual_request, label):
    data = visual_data(visual_request)
    data["visual_review"]["selected_visuals"][0]["number"] = label
    assert validate(data, visual_request)["visual_review"]["selected_visuals"][0]["number"] == label


def test_same_page_compound_and_unknown_figures_do_not_collide(visual_request):
    from copy import deepcopy

    data = visual_data(visual_request)
    first = data["visual_review"]["selected_visuals"][0]
    first.update(number=None, title_ja="Figure 4.4: 合成結果")
    second = dict(deepcopy(first), title_ja="Figure 4.5: 別の合成結果")
    data["visual_review"]["selected_visuals"].append(second)
    normalized = validate(data, visual_request)
    assert [v["number"] for v in normalized["visual_review"]["selected_visuals"]] == ["4.4", "4.5"]
    assert first["number"] is None  # 不採用の元回答を破壊しない
    first["title_ja"], second["title_ja"] = "番号不明の図", "別の番号不明の図"
    assert len(validate(data, visual_request)["visual_review"]["selected_visuals"]) == 2


def test_redundant_pages_and_references_are_normalized_without_inventing_pages(visual_request):
    from copy import deepcopy

    data = visual_data(visual_request)
    data["visual_review"]["pages_viewed"] = ["1", 2, "2"]
    selection = data["visual_review"]["selected_visuals"][0]
    selection.update(page="2", number="Fig. 2", finding_indices=[1, "1"])
    data["visual_review"]["selected_visuals"].append(deepcopy(selection))
    result = validate(data, visual_request)
    assert result["visual_review"]["pages_viewed"] == [1, 2]
    assert len(result["visual_review"]["selected_visuals"]) == 1
    assert result["visual_review"]["selected_visuals"][0]["finding_indices"] == [1]
    assert result["uncertainties_ja"] != data["uncertainties_ja"]
    assert validate(result, visual_request) == result
    data["visual_review"]["pages_viewed"] = ["1", "1"]
    with pytest.raises(ProviderError, match="未確認ページ"):
        validate(data, visual_request)


@pytest.mark.parametrize("bad", [None, [], "text", 1, True])
def test_non_object_ai_output_is_a_diagnostic_error(visual_request, bad):
    with pytest.raises(ProviderError, match="最上位"):
        validate(bad, visual_request)


@pytest.mark.parametrize("bad", [4.4, True, "../bad", "4/5", "", 0])
def test_ambiguous_or_unsafe_numbers_are_not_guessed(visual_request, bad):
    data = visual_data(visual_request)
    data["visual_review"]["selected_visuals"][0]["number"] = bad
    with pytest.raises(ProviderError, match="number"):
        validate(data, visual_request)


def test_visual_rejection_explains_reason_and_retains_usage(visual_request):
    data = visual_data(visual_request)
    data["visual_review"].update(status="unable", reason_ja="ページ2の図が判読できません。")
    usage = {"input_tokens": 1234, "output_tokens": 567}
    with pytest.raises(ProviderError, match="ページ2の図が判読") as error:
        validate_response(data, visual_request, usage=usage, elapsed_seconds=4.5)
    assert error.value.usage == usage
    assert error.value.elapsed_seconds == 4.5
    data["visual_review"].update(status="completed", reason_ja="", pages_viewed=[1])
    with pytest.raises(ProviderError, match="全2ページ中1ページ.*未確認ページ：2"):
        validate(data, visual_request)


@pytest.mark.parametrize("provider", [ClaudeCliProvider, CodexCliProvider])
def test_abstract_label_does_not_bypass_pdf_permission(small_config, visual_request, provider):
    with pytest.raises(ProviderError, match="全文送信"):
        provider(small_config).analyze(replace(visual_request, coverage="abstract"))


def test_document_snapshot_hash_and_limits(small_config, visual_request, tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    pdf = snapshot(visual_request.document, stage, small_config)
    assert pdf.read_bytes() == visual_request.document.path.read_bytes()
    images = page_images(pdf, stage)
    assert len(images) == 2 and all(p.is_file() for p in images)
    small_config.analysis.visual_max_pages = 1
    with pytest.raises(ProviderError, match="文字だけ"):
        snapshot(visual_request.document, stage, small_config)
    small_config.analysis.visual_max_pages = 60
    visual_request.document.path.write_bytes(b"changed")
    with pytest.raises(ProviderError, match="ハッシュ"):
        snapshot(visual_request.document, stage, small_config)


def test_large_pdf_repack_worker_preserves_original_and_cleans_temp(tmp_path):
    with tempfile.TemporaryDirectory(dir=tmp_path) as folder:
        cwd = Path(folder)
        pdf = cwd / "input.pdf"
        with pymupdf.open() as doc:
            p = doc.new_page()
            xref = doc.get_new_xref()
            doc.update_object(xref, "<< >>")
            doc.update_stream(xref, b"%" + b"x" * (17 * 1024 * 1024) + b"\n", compress=0)
            p.set_contents(xref)
            p.insert_text((40, 40), "Keep this text")
            doc.save(pdf, deflate=False)
        original = hashlib.sha256(pdf.read_bytes()).hexdigest()
        parts, method = prepare_claude_pdf(pdf, cwd)
        assert method == "lossless_repack"
        assert len(parts) == 1 and parts[0].path.stat().st_size < 1024 * 1024
        assert hashlib.sha256(pdf.read_bytes()).hexdigest() == original
        with pymupdf.open(parts[0].path) as doc:
            assert len(doc) == 1 and "Keep this text" in doc[0].get_text()
    assert not list(tmp_path.iterdir())


def test_pdf_image_rewrite_keeps_text_pages_and_source(tmp_path, monkeypatch):
    monkeypatch.setattr("paperflow.agents.document.CLAUDE_PDF_BYTES", 2 * 1024 * 1024)
    pdf = tmp_path / "input.pdf"
    with pymupdf.open() as doc:
        p = doc.new_page(width=400, height=400)
        pixels = random.Random(53).randbytes(1200 * 1200 * 3)
        pix = pymupdf.Pixmap(pymupdf.csRGB, 1200, 1200, pixels, False)
        p.insert_image(p.rect, pixmap=pix)
        p.insert_text((40, 40), "Preserve vector text")
        doc.save(pdf)
    original = pdf.read_bytes()
    parts, method = claude_pdf_parts(pdf, tmp_path)
    assert method == "images_150dpi_jpeg85"
    assert parts[0].path.stat().st_size <= 2 * 1024 * 1024
    assert pdf.read_bytes() == original
    with pymupdf.open(parts[0].path) as doc:
        assert len(doc) == 1
        assert "Preserve vector text" in doc[0].get_text()


def test_pdf_parts_include_every_page_with_original_numbering(tmp_path):
    pdf = tmp_path / "input.pdf"
    with pymupdf.open() as doc:
        for number in range(101):
            doc.new_page().insert_text((40, 40), f"Original page {number + 1}")
        doc.save(pdf)
    parts, method = prepare_claude_pdf(pdf, tmp_path)
    assert [(p.first_page, p.last_page) for p in parts] == [(1, 100), (101, 101)]
    texts = []
    for part in parts:
        with pymupdf.open(part.path) as doc:
            texts.extend(p.get_text().strip() for p in doc)
    assert texts == [f"Original page {number + 1}" for number in range(101)]


def test_pdf_still_too_large_stops_before_llm(small_config, visual_request, monkeypatch):
    small_config.analysis.policy = "fulltext_external"
    small_config.analysis.allow_external_fulltext = True
    monkeypatch.setattr("paperflow.agents.claude_cli.resolve_command", lambda _: "claude")
    monkeypatch.setattr("paperflow.agents.claude_cli.checked_help", lambda *a: "--input-format")

    def rejected(*args):
        raise ProviderError("cli_input_limit", "合成上限に収まらないため送信しません")

    def forbidden(*a, **k):
        raise AssertionError("容量超過したPDFをLLMへ送りました")

    monkeypatch.setattr("paperflow.agents.document.prepare_claude_pdf", rejected)
    monkeypatch.setattr("paperflow.agents.claude_cli.run_bounded", forbidden)
    with pytest.raises(ProviderError, match="送信しません"):
        ClaudeCliProvider(small_config).analyze(visual_request)
    assert not list((small_config.storage.data_dir / "staging").iterdir())


def test_optimized_pdf_quality_is_disclosed(small_config, visual_request, monkeypatch):
    from paperflow.agents.document import PDFPart

    small_config.analysis.policy = "fulltext_external"
    small_config.analysis.allow_external_fulltext = True
    monkeypatch.setattr("paperflow.agents.claude_cli.resolve_command", lambda _: "claude")
    monkeypatch.setattr("paperflow.agents.claude_cli.checked_help", lambda *a: "--input-format")
    monkeypatch.setattr(
        "paperflow.agents.document.prepare_claude_pdf",
        lambda pdf, cwd: ([PDFPart(pdf, 1, 2)], "images_150dpi_jpeg85"),
    )

    def run(argv, **kwargs):
        assert "150dpi" in json.loads(kwargs["text"])["message"]["content"][-1]["text"]
        return json.dumps(dict(type="result", structured_output=visual_data(visual_request)))

    monkeypatch.setattr("paperflow.agents.claude_cli.run_bounded", run)
    data = ClaudeCliProvider(small_config).analyze(visual_request).data
    assert any("150dpi" in note for note in data["uncertainties_ja"])
    assert data["visual_review"]["pages_viewed"] == [1, 2]


def test_agent_chooses_figure_instead_of_first_caption(visual_request, tmp_path):
    selection = visual_data(visual_request)["visual_review"]["selected_visuals"]
    data = extract_visuals(
        visual_request.document.path,
        tmp_path / "visuals",
        expected_sha256=visual_request.document.sha256,
        source_id="paper:test",
        selections=selection,
    )
    assert data["selection_method"] == "agent"
    assert len(data["items"]) == 1
    assert data["items"][0]["page"] == 2 and data["items"][0]["number"] == 2
    assert data["items"][0]["selection"]["explanation_ja"] == selection[0]["explanation_ja"]
    assert data["items"][0]["mode"] == "crop"


def test_uncertain_agent_figure_uses_full_page(visual_request, tmp_path):
    selection = visual_data(visual_request)["visual_review"]["selected_visuals"]
    selection[0]["number"] = 99
    data = extract_visuals(
        visual_request.document.path,
        tmp_path / "visuals",
        expected_sha256=visual_request.document.sha256,
        source_id="paper:test",
        selections=selection,
    )
    assert data["items"][0]["mode"] == "page-context"


@pytest.mark.parametrize(
    "name,provider", [("claude", ClaudeCliProvider), ("codex", CodexCliProvider)]
)
def test_providers_deliver_visual_content_with_tools_disabled(
    small_config, visual_request, monkeypatch, name, provider
):
    small_config.analysis.policy = "fulltext_external"
    small_config.analysis.allow_external_fulltext = True
    module = f"paperflow.agents.{name}_cli"
    monkeypatch.setattr(module + ".resolve_command", lambda _: name)
    monkeypatch.setattr(module + ".checked_help", lambda *a: "--input-format --image")
    data = visual_data(visual_request)

    def run(argv, **kwargs):
        assert str(visual_request.document.path) not in kwargs["text"]
        if name == "claude":
            assert argv[argv.index("--tools") + 1] == ""
            message = json.loads(kwargs["text"])
            pdf = message["message"]["content"][0]
            assert pdf["type"] == "document"
            assert (
                base64.b64decode(pdf["source"]["data"]) == visual_request.document.path.read_bytes()
            )
            assert message["message"]["content"][-1]["type"] == "text"
            assert "attached_pdf" in message["message"]["content"][-1]["text"]
            assert visual_request.text not in message["message"]["content"][-1]["text"]
            assert json.loads(argv[argv.index("--json-schema") + 1]) == VISUAL_SCHEMA
            return json.dumps(dict(type="result", structured_output=data))
        assert "features.shell_tool=false" in argv
        images = [Path(argv[i + 1]) for i, a in enumerate(argv) if a == "--image"]
        assert len(images) == 2 and all(p.is_file() for p in images)
        assert (
            json.loads(Path(argv[argv.index("--output-schema") + 1]).read_text()) == VISUAL_SCHEMA
        )
        Path(argv[argv.index("-o") + 1]).write_text(json.dumps(data), encoding="utf-8")
        return ""

    monkeypatch.setattr(module + ".run_bounded", run)
    assert provider(small_config).analyze(visual_request).data == data


def test_pipeline_visual_analysis_reuses_cache_and_preserves_original(small_config):
    cfg = small_config
    cfg.analysis.policy = "fulltext_external"
    cfg.analysis.allow_external_fulltext = True
    pdf = next(cfg.source.paperpile_dir.glob("*.pdf"))
    before = pdf.read_bytes()
    calls = []

    class SyntheticProvider:
        def analyze(self, request):
            calls.append(request)
            assert request.document is not None
            return AnalysisResult(visual_data(request), "claude", "synthetic-model")

    pipeline = Pipeline(cfg, provider=SyntheticProvider())
    pipeline.run(scan_only=True)
    first = pipeline.run(provider_name="claude", export=False)
    second = pipeline.run(provider_name="claude", export=False)
    assert first["analyzed"] == 1 and second["analyzed"] == 0 and len(calls) == 1
    assert pdf.read_bytes() == before
    with Store(cfg.storage.database, readonly=True) as store:
        assert (
            store.rows("SELECT schema_version FROM analysis_runs")[0]["schema_version"]
            == "v2-visual"
        )


def test_image_budget_does_not_silently_send_only_first_pages(visual_request, tmp_path):
    with pytest.raises(ProviderError, match="文字だけ"):
        page_images(visual_request.document.path, tmp_path, max_bytes=1)


def test_library_based_input_limits_are_configurable():
    from paperflow.config import Analysis

    cfg = Analysis()
    assert (
        cfg.visual_max_pages,
        cfg.visual_max_pdf_mb,
        cfg.visual_max_image_mb,
        cfg.max_chars,
    ) == (300, 64, 128, 3000000)
    # 旧バリデータの100ページ/24MiB/100MiB/100万文字の制限へ戻らない。
    Analysis(visual_max_pages=300, visual_max_pdf_mb=64, visual_max_image_mb=128, max_chars=3000000)


def test_large_synthetic_pdf_is_within_measured_limit(small_config, tmp_path):
    pdf = tmp_path / "large-synthetic.pdf"
    with pymupdf.open() as doc:
        for number in range(264):
            p = doc.new_page()
            p.insert_text((40, 40), f"Synthetic page {number + 1}")
        doc.save(pdf)
    document = PDFDocument(pdf, hashlib.sha256(pdf.read_bytes()).hexdigest(), 264)
    stage = tmp_path / "stage"
    stage.mkdir()
    assert snapshot(document, stage, small_config).is_file()


@pytest.mark.parametrize(
    "name,provider", [("claude", ClaudeCliProvider), ("codex", CodexCliProvider)]
)
def test_visual_stage_is_removed_after_failure(
    small_config, visual_request, monkeypatch, name, provider
):
    small_config.analysis.policy = "fulltext_external"
    small_config.analysis.allow_external_fulltext = True
    module = f"paperflow.agents.{name}_cli"
    monkeypatch.setattr(module + ".resolve_command", lambda _: name)
    monkeypatch.setattr(module + ".checked_help", lambda *a: "--input-format --image --json")
    stages = []

    def fail(argv, **kwargs):
        stages.append(kwargs["cwd"])
        assert (kwargs["cwd"] / "input.pdf").exists()
        if name == "codex":
            assert list(kwargs["cwd"].glob("page-*.png"))
        raise ProviderError("timeout", "合成タイムアウト", True)

    monkeypatch.setattr(module + ".run_bounded", fail)
    with pytest.raises(ProviderError, match="合成タイムアウト"):
        provider(small_config).analyze(visual_request)
    assert stages and all(not stage.exists() for stage in stages)
    assert not list((small_config.storage.data_dir / "staging").iterdir())


def test_visual_stage_removed_if_image_budget_exceeded(small_config, visual_request, monkeypatch):
    small_config.analysis.policy = "fulltext_external"
    small_config.analysis.allow_external_fulltext = True
    monkeypatch.setattr("paperflow.agents.codex_cli.resolve_command", lambda _: "codex")
    monkeypatch.setattr("paperflow.agents.codex_cli.checked_help", lambda *a: "--image --json")
    from paperflow.agents import document

    real = document.page_images
    monkeypatch.setattr(
        document, "page_images", lambda pdf, cwd, **kwargs: real(pdf, cwd, max_bytes=1)
    )
    with pytest.raises(ProviderError, match="上限"):
        CodexCliProvider(small_config).analyze(visual_request)
    assert not list((small_config.storage.data_dir / "staging").iterdir())
