import pytest
import yaml
from conftest import tree
from create_demo import make_demo

from paperflow.config import SettingsError, load_config
from paperflow.setup import initialize


def setup_project(tmp_path):
    path = make_demo(tmp_path / "初期設定", count=1)
    values = yaml.safe_load(path.read_text(encoding="utf-8"))
    values["destination"]["allow_vault_write"] = False
    template = "# 初期設定のコメントを保持\n" + yaml.safe_dump(
        values, allow_unicode=True, sort_keys=False
    )
    root = path.parent
    path.unlink()
    (root / "config.example.yaml").write_text(template, encoding="utf-8")
    (root / "pyproject.toml").write_text("# 合成プロジェクト\n", encoding="utf-8")
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    return root


def test_new_config_is_safe_and_preserves_template_without_cli_or_vault_writes(
    tmp_path, monkeypatch
):
    root = setup_project(tmp_path)
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("CLIを起動しない"))
    before = tree(root)
    result = initialize(root)
    assert result["created"] is True
    config = root / "config.production.yaml"
    assert config.read_bytes() == (root / "config.example.yaml").read_bytes()
    cfg = load_config(config)
    assert not cfg.analysis.allow_external_fulltext
    assert not cfg.destination.allow_vault_write
    after = tree(root)
    assert set(after) - set(before) == {"config.production.yaml"}
    assert all(after[name] == content for name, content in before.items())


def test_setup_is_idempotent_and_does_not_reset_existing_permissions_or_settings(tmp_path):
    root = setup_project(tmp_path)
    initialize(root)
    path = root / "config.production.yaml"
    original = path.read_text(encoding="utf-8").replace(
        "allow_vault_write: false", "allow_vault_write: true"
    )
    path.write_text(original + "\n# PC固有設定は保持する\n", encoding="utf-8")
    before = tree(root)
    assert initialize(root)["created"] is False
    assert tree(root) == before


def test_unsafe_template_and_missing_source_fail_without_creating_config(tmp_path):
    root = setup_project(tmp_path)
    template = root / "config.example.yaml"
    template.write_text(
        template.read_text(encoding="utf-8").replace(
            "allow_vault_write: false", "allow_vault_write: true"
        ),
        encoding="utf-8",
    )
    with pytest.raises(SettingsError, match="false"):
        initialize(root)
    assert not (root / "config.production.yaml").exists()
    (root / "uv.lock").unlink()
    with pytest.raises(SettingsError, match="不足"):
        initialize(root)
