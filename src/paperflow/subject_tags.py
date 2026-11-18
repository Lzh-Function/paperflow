"""管理主題語をObsidianの階層タグへ対応させる。"""

import json
import re

import yaml


def subject_tags(topics):
    if not isinstance(topics, list) or any(
        not isinstance(t, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", t) for t in topics
    ):
        raise ValueError("主題語はASCIIの管理slug一覧で指定してください")
    return list(dict.fromkeys("topic/" + t for t in topics))


def register_topics(root, topics):
    subject_tags(topics)
    path = root / "00_System/Dictionaries/tag-taxonomy.yml"
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8-sig")
    data = yaml.safe_load(text)
    allowed = data["allowed"]
    current = allowed.get("topic", [])
    if not isinstance(current, list):
        raise ValueError("Vaultのtopicタグ辞書が不正です")
    combined = list(dict.fromkeys([*current, *topics]))
    if combined == current:
        return False
    tree = yaml.compose(text)
    nodes = {k.value: v for k, v in tree.value}
    children = {k.value: v for k, v in nodes["allowed"].value}
    edits = []
    if "topic" in children:
        node = children["topic"]
        previous = text[node.start_mark.index : node.end_mark.index]
        value = json.dumps(combined, ensure_ascii=False) + ("\n" if previous.endswith("\n") else "")
        edits.append((node.start_mark.index, node.end_mark.index, value))
    else:
        at = nodes["allowed"].end_mark.index
        value = "  topic:\n" + "".join("    - " + t + "\n" for t in combined)
        edits.append((at, at, value))
    if isinstance(data.get("version"), int) and data["version"] < 4:
        node = nodes["version"]
        edits.append((node.start_mark.index, node.end_mark.index, "4"))
    for start, end, value in sorted(edits, reverse=True):
        text = text[:start] + value + text[end:]
    revised = yaml.safe_load(text)
    if revised["allowed"]["topic"] != combined:
        raise ValueError("Vaultの主題タグ辞書を変更できませんでした")
    path.write_text(text, encoding="utf-8", newline="\n")
    return True


def patch_note(raw):
    """本文・他プロパティ・BOM・改行を保持し、生成ノートのtagsだけ変更。"""
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    opening = re.match(r"---\r?\n", text)
    if not opening:
        return raw
    closing = re.search(r"(?m)^---[ \t]*(?:\r?\n|$)", text[opening.end() :])
    if not closing:
        return raw
    end = opening.end() + closing.start()
    header = text[opening.end() : end]
    data = yaml.safe_load(header)
    if (
        not isinstance(data, dict)
        or data.get("paperflow_managed") is not True
        or data.get("owner") != "agent"
        or data.get("agent_editable") is not True
    ):
        return raw
    tags = data.get("tags", [])
    if not isinstance(tags, list):
        raise ValueError("生成ノートのtagsが一覧ではありません")
    new = list(dict.fromkeys([*tags, *subject_tags(data.get("topics", []))]))
    if new == tags:
        return raw
    newline = "\r\n" if "\r\n" in header else "\n"
    nodes = {k.value: (k, v) for k, v in yaml.compose(header).value}
    if "tags" in nodes:
        key, value = nodes["tags"]
        previous = header[key.start_mark.index : value.end_mark.index]
        replacement = "tags: " + json.dumps(new, ensure_ascii=False)
        if previous.endswith("\n"):
            replacement += newline
        updated = header[: key.start_mark.index] + replacement + header[value.end_mark.index :]
    else:
        updated = header + "tags: " + json.dumps(new, ensure_ascii=False) + newline
    expected = dict(data, tags=new)
    if yaml.safe_load(updated) != expected:
        raise ValueError("tags以外のメタデータが変わるため停止しました")
    result = text[: opening.end()] + updated + text[end:]
    return (b"\xef\xbb\xbf" if bom else b"") + result.encode("utf-8")
