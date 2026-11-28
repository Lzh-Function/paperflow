"""Google Driveの表示名変更を、ファイル内容の変更と区別する。"""

from pathlib import Path, PureWindowsPath

from paperflow.paths import resolve_path


def drive_path(value):
    """ドライブ直下の旧表示名だけを読み替える。任意のフォルダ名は変更しない。"""
    path = PureWindowsPath(str(value))
    if (
        len(path.drive) == 2
        and path.drive.endswith(":")
        and path.root
        and len(path.parts) > 1
        and path.parts[1] == "マイドライブ"
    ):
        return str(PureWindowsPath(path.anchor, "My Drive", *path.parts[2:]))
    return str(value)


def relative_path(value, root):
    path = resolve_path(Path(drive_path(value)))
    base = resolve_path(Path(drive_path(root)))
    if not path.is_relative_to(base):
        raise ValueError("共有データに原本/Vault外のパスがあります")
    return path.relative_to(base).as_posix()


def same_binding(first, second):
    if not isinstance(first, dict) or first.keys() != second.keys():
        return False
    for key in first:
        a, b = first[key], second[key]
        if a is None or b is None:
            if a != b:
                return False
        else:
            a, b = drive_path(a), drive_path(b)
            if PureWindowsPath(a).drive or PureWindowsPath(b).drive:
                if PureWindowsPath(a) != PureWindowsPath(b):
                    return False
            elif Path(a) != Path(b):
                return False
    return True
