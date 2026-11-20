import json
from pathlib import Path

import pytest
from conftest import tree
from test_agents import request_data as abstract_fixture
from test_agy import channel_fixture
from test_vision import visual_data
from test_vision import visual_request as visual_fixture

from paperflow.agents.agy_cli import AgyCliProvider
from paperflow.agents.base import ProviderError
from paperflow.agents.claude_cli import ClaudeCliProvider
from paperflow.agents.codex_cli import CodexCliProvider
from paperflow.agents.fake import FakeProvider
from paperflow.agents.json_response import (
    parse_analysis_response,
    parse_response,
    response_error,
)
from paperflow.agents.schema import validate
from paperflow.pipeline import Pipeline
from paperflow.store import Store


@pytest.fixture
def request_data():
    return abstract_fixture.__wrapped__()


def broken_response(data):
    data = dict(data, approach_ja=r"合成立体記号（@、@@、/、\等）を保持する。")
    raw = json.dumps(data, ensure_ascii=False).replace(r"\\等", r"\等")
    return data, raw


def test_unescaped_smiles_backslash_is_preserved_and_correction_recorded(request_data):
    data, raw = broken_response(FakeProvider().analyze(request_data).data)
    with pytest.raises(json.JSONDecodeError):
        parse_response(raw)  # CLIの外枠には寛容な補正を適用しない。
    result = validate(parse_analysis_response(raw), request_data)
    assert result["approach_ja"] == data["approach_ja"]
    assert result["uncertainties_ja"][:-1] == data["uncertainties_ja"]
    assert "バックスラッシュ1箇所" in result["uncertainties_ja"][-1]
    assert parse_analysis_response(json.dumps(result, ensure_ascii=False)) == result


@pytest.mark.parametrize("control", [chr(i) for i in range(32)])
def test_raw_control_characters_in_values_roundtrip_without_deleting_text(request_data, control):
    data = FakeProvider().analyze(request_data).data
    data["approach_ja"] = "合成前半" + control + "後半"
    escaped = json.dumps(control)[1:-1]
    raw = json.dumps(data, ensure_ascii=False).replace(escaped, control)
    result = validate(parse_analysis_response(raw), request_data)
    assert result["approach_ja"] == data["approach_ja"]
    assert "制御文字1箇所" in result["uncertainties_ja"][-1]


@pytest.mark.parametrize(
    "text",
    [
        r"F/C=C\F",
        r"C:\new\test\sample.pdf",
        r"\alpha + \beta = \frac{1}{2}",
        '合成の引用 "quoted" と「引用」・“quotes”・αβ・μM・😀',
        "改行\nタブ\t復帰\rバックスペース\b",
        r"literal \n \t \u1234 と末尾" + "\\",
    ],
)
def test_valid_special_symbols_and_escapes_are_never_reinterpreted(request_data, text):
    data = FakeProvider().analyze(request_data).data
    data["approach_ja"] = text
    assert parse_analysis_response(json.dumps(data, ensure_ascii=False)) == data
    assert parse_analysis_response(json.dumps(data, ensure_ascii=True)) == data
    data, raw = broken_response(data)
    result = parse_analysis_response(raw)
    assert result["approach_ja"] == data["approach_ja"]
    # 同じ回答に不正なバックスラッシュがあっても、別の値の有効な表記を再解釈しない。
    data["tldr_ja"] = text
    _, raw = broken_response(data)
    assert parse_analysis_response(raw)["tldr_ja"] == text


@pytest.mark.parametrize(
    "wrapper", [lambda s: s, lambda s: "\ufeff" + s, lambda s: "```json\n" + s + "\n```"]
)
def test_representation_correction_survives_supported_wrappers(request_data, wrapper):
    data, raw = broken_response(FakeProvider().analyze(request_data).data)
    assert parse_analysis_response(wrapper(raw))["approach_ja"] == data["approach_ja"]


@pytest.mark.parametrize("count", range(1, 7))
def test_consecutive_backslashes_keep_their_count(request_data, count):
    data = FakeProvider().analyze(request_data).data
    data["approach_ja"] = "合成の記号" + "\\" * count + "等を保持"
    raw = json.dumps(data, ensure_ascii=False)
    # 末尾の明確な不正エスケープ1個だけを作る。
    raw = raw.replace("\\" * (count * 2) + "等", "\\" * (count * 2 - 1) + "等")
    assert parse_analysis_response(raw)["approach_ja"] == data["approach_ja"]


@pytest.mark.parametrize(
    "raw",
    [
        r'{"text":"\u123"}',
        r'{"text":"\uZZZZ"}',
        r'{"text":"dangling\"}',
        '{"text":"raw "quote""}',
        '{"text":"x",}',
        "{“text”:“合成”}",
        "{}\n{}",
        "説明 {}",
        r'{"text":"\q","text":"duplicate"}',
        r'{"bad\key":"x"}',
        '{"bad\nkey":"x"}',
        r'{"text":"\q","value":NaN}',
        r'{"text":"\q","value":1e999}',
        r'{"text":"\q","extra":',
        r'{"text":"\ud800"}',
        r'{"text":"\q","extra":"\udfff"}',
    ],
)
def test_ambiguous_structure_keys_unicode_and_numbers_remain_rejected(raw):
    with pytest.raises(ValueError):
        parse_analysis_response(raw)


def test_failed_correction_retains_both_texts_and_identifies_error_position():
    raw = '{"text":"合成\\q\n改行", "unfinished":'
    with pytest.raises(ValueError) as failure:
        parse_analysis_response(raw)
    error = response_error("合成の不採用回答", failure.value, raw=raw)
    assert error.raw_response == raw
    assert error.normalized_response != raw
    assert error.parse_error["position_source"] == "normalized_response"
    assert error.parse_error["line"] == 1


@pytest.mark.parametrize("wrapper", [lambda s: "\ufeff" + s, lambda s: "```json\n" + s + "\n```"])
def test_wrapped_failure_identifies_the_json_fragment_as_position_source(wrapper):
    fragment = '{"text":"合成の途中'
    raw = wrapper(fragment)
    with pytest.raises(ValueError) as failure:
        parse_analysis_response(raw)
    error = response_error("合成の不採用回答", failure.value, raw=raw)
    assert error.raw_response == raw and error.normalized_response == fragment
    assert error.parse_error["position_source"] == "normalized_response"


def test_normalization_does_not_supply_missing_fields_or_bypass_semantics(request_data):
    data, _ = broken_response(FakeProvider().analyze(request_data).data)
    data["tags"] = ["not-allowed"]
    raw = json.dumps(data, ensure_ascii=False).replace(r"\\等", r"\等")
    with pytest.raises(ProviderError, match="管理語彙"):
        validate(parse_analysis_response(raw), request_data)
    with pytest.raises(ProviderError, match="Schema"):
        validate(parse_analysis_response(r'{"text":"\q"}'), request_data)


@pytest.mark.parametrize("provider", ["agy", "codex", "claude"])
def test_every_provider_normalizes_synthetic_response_and_cleans_working_files(
    small_config, request_data, monkeypatch, provider
):
    data, raw = broken_response(FakeProvider().analyze(request_data).data)
    if provider == "agy":
        calls, dirs = channel_fixture(monkeypatch, data, response_text=raw)
        result = AgyCliProvider(small_config).analyze(request_data)
        assert sum(c["method"] == "session/prompt" for c in calls) == 1
        assert not any(p.exists() for p in dirs)
    else:
        module = "paperflow.agents." + provider + "_cli"
        monkeypatch.setattr(module + ".resolve_command", lambda _: provider)
        monkeypatch.setattr(module + ".checked_help", lambda *a: "")
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            if provider == "claude":
                return json.dumps(dict(structured_output=raw))
            Path(argv[argv.index("-o") + 1]).write_text(raw, encoding="utf-8")
            return "{}"

        monkeypatch.setattr(module + ".run_bounded", run)
        cls = ClaudeCliProvider if provider == "claude" else CodexCliProvider
        result = cls(small_config).analyze(request_data)
        assert len(calls) == 1
    assert result.data["approach_ja"] == data["approach_ja"]
    assert "バックスラッシュ1箇所" in result.data["uncertainties_ja"][-1]
    assert not list((small_config.storage.data_dir / "staging").iterdir())


def test_agy_visual_normalization_keeps_complete_reading_requirement(
    small_config, tmp_path, monkeypatch
):
    request = visual_fixture.__wrapped__(tmp_path)
    small_config.analysis.policy = "fulltext_external"
    small_config.analysis.allow_external_fulltext = True
    data, raw = broken_response(visual_data(request))
    channel_fixture(monkeypatch, data, response_text=raw)
    assert AgyCliProvider(small_config).analyze(request).data["visual_review"]["pages_viewed"] == [
        1,
        2,
    ]
    channel_fixture(monkeypatch, data, mode="missing-read", response_text=raw)
    with pytest.raises(ProviderError) as error:
        AgyCliProvider(small_config).analyze(request)
    assert error.value.category == "visual_reading"
    assert error.value.raw_response == raw


@pytest.mark.parametrize("provider", ["agy", "codex", "claude"])
def test_unrepairable_response_retains_raw_text_reason_and_usage(
    small_config, request_data, monkeypatch, provider
):
    raw = '{"approach_ja":"合成の途中'
    if provider == "agy":
        channel_fixture(monkeypatch, {}, response_text=raw)
        cls = AgyCliProvider
    else:
        module = "paperflow.agents." + provider + "_cli"
        monkeypatch.setattr(module + ".resolve_command", lambda _: provider)
        monkeypatch.setattr(module + ".checked_help", lambda *a: "")

        def run(argv, **kwargs):
            if provider == "claude":
                return json.dumps(dict(structured_output=raw, usage=dict(input_tokens=12)))
            Path(argv[argv.index("-o") + 1]).write_text(raw, encoding="utf-8")
            return json.dumps(dict(type="turn.completed", usage=dict(input_tokens=12)))

        monkeypatch.setattr(module + ".run_bounded", run)
        cls = ClaudeCliProvider if provider == "claude" else CodexCliProvider
    with pytest.raises(ProviderError) as error:
        cls(small_config).analyze(request_data)
    exc = error.value
    assert exc.category == "invalid_json" and exc.raw_response == raw
    assert exc.parse_error["line"] == 1 and "列" in str(exc)
    assert exc.input_receipt["provider"] == provider
    assert exc.usage["input_tokens"] == (50 if provider == "agy" else 12)
    assert not list((small_config.storage.data_dir / "staging").iterdir())


@pytest.mark.parametrize(
    "raw", ['{"approach_ja":"合成の途中\ud800', '{"text":"合成\\q\n改行", "unfinished":']
)
def test_pipeline_keeps_malformed_raw_response_without_export_or_retry(small_config, raw):
    class BrokenProvider:
        def analyze(self, request):
            try:
                parse_analysis_response(raw)
            except ValueError as cause:
                error = response_error("合成JSONを読み取れません", cause, raw=raw)
                error.usage = dict(input_tokens=123, output_tokens=45)
                error.elapsed_seconds = 2.0
                raise error from cause

    original = tree(small_config.source.paperpile_dir)
    before = tree(small_config.destination.vault_dir)
    Pipeline(small_config).run(scan_only=True)
    result = Pipeline(small_config, BrokenProvider()).run(export=False)
    assert len(result["failures"]) == 1
    with Store(small_config.storage.database, readonly=True) as store:
        run = store.rows("SELECT * FROM analysis_runs")[0]
        assert run["status"] == "failed" and run["error_type"] == "invalid_json"
        assert run["retryable"] == 0 and store.rows("SELECT * FROM exports") == []
        assert len(store.rows("SELECT * FROM analysis_usage")) == 1
    path = small_config.storage.data_dir / "diagnostics" / (run["run_id"] + ".rejected.json")
    diagnostic = json.loads(path.read_text(encoding="utf-8"))
    assert diagnostic["accepted"] is False and diagnostic["response"] is None
    assert diagnostic["raw_response"] == raw
    assert diagnostic["parse_error"]["line"] == 1
    if "normalized_response" in diagnostic:
        assert diagnostic["normalized_response"] != raw
        assert diagnostic["parse_error"]["position_source"] == "normalized_response"
    assert tree(small_config.source.paperpile_dir) == original
    assert tree(small_config.destination.vault_dir) == before
