"""GUIの設定編集。コメントを保ち、検証後に置換する。"""

import json
import os
import tempfile
from pathlib import Path

import yaml

from paperflow.config import Config, SettingsError, load_config
from paperflow.obsidian.writer import WriteConflict, local_lock


def config_values(path):
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8-sig"))
    return Config.model_validate(raw).model_dump(mode="json")


def save_changes(path, changes, *, expected_text=None):
    path = Path(path).resolve()
    config = load_config(path)
    if (
        (os.name == "nt" and path.drive.upper() != "C:")
        or any(
            path.is_relative_to(p.resolve())
            for p in [config.source.paperpile_dir, config.destination.vault_dir]
        )
        or any(p.casefold() == ".obsidian" for p in path.parts)
    ):
        raise SettingsError("設定ファイルはC:のPaperpile・Vault以外の場所へ保存してください")
    with local_lock(config.storage.data_dir):
        original = path.read_text(encoding="utf-8-sig")
        if expected_text is not None and original != expected_text:
            raise WriteConflict("設定ファイルが別の画面で変更されました。読み込み直してください")
        values = config_values(path)
        nodes = {}
        sections = {}
        tree = yaml.compose(original)
        for key, section in tree.value:
            sections[key.value] = section
            for field, value in section.value:
                nodes[key.value + "." + field.value] = value
        edits = []
        missing = {}
        for dotted, value in changes.items():
            section, key = dotted.split(".", 1)
            if section not in values or key not in values[section]:
                raise SettingsError("未対応の設定項目です: " + dotted)
            if values[section][key] == value:
                continue
            values[section][key] = value
            text = json.dumps(value, ensure_ascii=False)
            if dotted in nodes:
                node = nodes[dotted]
                old = original[node.start_mark.index : node.end_mark.index]
                if old.endswith("\n"):
                    text += "\n"
                edits.append((node.start_mark.index, node.end_mark.index, text))
            else:
                missing.setdefault(section, []).append("  " + key + ": " + text + "\n")
        for section, lines in missing.items():
            if section in sections:
                at = sections[section].end_mark.index
                edits.append((at, at, "\n" + "".join(lines)))
            else:
                edits.append(
                    (len(original), len(original), "\n" + section + ":\n" + "".join(lines))
                )
        Config.model_validate(values)
        revised = original
        for start, end, text in sorted(edits, reverse=True):
            revised = revised[:start] + text + revised[end:]
        if Config.model_validate(yaml.safe_load(revised)).model_dump(mode="json") != values:
            raise SettingsError(
                "このYAML形式を変更できません。設定ファイルをエディタで確認してください"
            )
        fd, name = tempfile.mkstemp(prefix=".pf-settings-", suffix=".yaml", dir=path.parent)
        temp = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(revised)
                handle.flush()
                os.fsync(handle.fileno())
            load_config(temp)
            if path.read_text(encoding="utf-8-sig") != original:
                raise WriteConflict("保存前に設定ファイルが変更されました")
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
    return load_config(path)
