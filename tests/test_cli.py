import json

import pytest
import yaml
from click.testing import CliRunner

from paperflow.cli import app
from paperflow.config import SettingsError, load_config
from paperflow.pipeline import Pipeline


def test_cli_dryrun_status_report_and_global_options(small_config):
    path = small_config.source.paperpile_dir.parent / "config.yaml"
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(path), "--json", "sync", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["dry_run"] is True
    assert not small_config.storage.database.exists()
    result = runner.invoke(app, ["--config", str(path), "status"])
    assert result.exit_code == 0
    assert json.loads(result.output)["stages"] == []
    assert not small_config.storage.database.exists()
    Pipeline(small_config).run(scan_only=True)
    Pipeline(small_config).run()
    for args in (
        ["report", "--min-priority", "1", "--write"],
        ["report", "--min-priority", "1", "--write"],
        ["status"],
        ["watch", "--once"],
    ):
        result = runner.invoke(app, ["--config", str(path), *args])
        assert result.exit_code == 0, (result.output, result.exception)
    assert (small_config.destination.vault_dir / "PaperFlow/Reports/top-rated.md").exists()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d["source"].update(read_only=False),
        lambda d: d["destination"].update(copy_pdf=True),
        lambda d: d["analysis"].update(policy="fulltext_external", allow_external_fulltext=False),
        lambda d: d["storage"].update(database=d["source"]["paperpile_dir"] + "/state.db"),
        lambda d: d["destination"].update(namespace="../Wiki"),
    ],
)
def test_config_fails_closed(small_config, mutation):
    path = small_config.source.paperpile_dir.parent / "config.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutation(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    with pytest.raises(SettingsError):
        load_config(path)


def test_generated_frontmatter_wiki_contract(small_config):
    Pipeline(small_config).run(scan_only=True)
    Pipeline(small_config).run()
    allowed = {"note/output", "note/personal", "area/academic", "source/paper"}
    allowed |= {"topic/" + t for t in small_config.taxonomy()}
    for note in small_config.destination.vault_dir.rglob("*.md"):
        data = yaml.safe_load(note.read_text(encoding="utf-8").split("---", 2)[1])
        assert data["area"] == "academic"
        assert set(data["tags"]) <= allowed
        assert {t for t in data["tags"] if t.startswith("topic/")} == {
            "topic/" + t for t in data["topics"]
        }
        assert data["source_ids"] == []  # external source is not falsely registered
        assert data["registration_status"] == "external-unregistered"


def test_installed_cli_preserves_partial_failure_exit_code(small_config):
    import subprocess
    import sys

    pdf = next(small_config.source.paperpile_dir.glob("*.pdf"))
    pdf.write_bytes(b"synthetic broken PDF")
    Pipeline(small_config).run(scan_only=True)
    config_path = small_config.source.paperpile_dir.parent / "config.yaml"
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "paperflow", "--config", str(config_path), "sync"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 3, result.stderr
    assert json.loads(result.stdout)["failures"]
