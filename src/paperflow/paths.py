"""Cryptomatorのローカル仮想ドライブでの限定的なパス解決互換。"""

import os
import runpy
import stat
import sys
from contextlib import contextmanager
from pathlib import Path

_NATIVE_RESOLVE = Path.resolve


def _cryptomator_volume(root):
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_volume = kernel.GetVolumeInformationW
    get_volume.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    get_volume.restype = wintypes.BOOL
    filesystem = ctypes.create_unicode_buffer(256)
    serial, component, flags = (wintypes.DWORD() for _ in range(3))
    if not get_volume(
        str(root),
        None,
        0,
        ctypes.byref(serial),
        ctypes.byref(component),
        ctypes.byref(flags),
        filesystem,
        len(filesystem),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    if filesystem.value.casefold() != "cryptofs":
        return False
    # SUBST等の別名を通常の別ボリュームと誤認しない。
    query = kernel.QueryDosDeviceW
    query.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    query.restype = wintypes.DWORD
    device = ctypes.create_unicode_buffer(32768)
    if not query(root.drive, device, len(device)):
        raise ctypes.WinError(ctypes.get_last_error())
    return device.value.startswith("\\Device\\")


def resolve_path(value, *, strict=False):
    """通常のresolveを優先。WinError 1005 + cryptoFsだけを補完する。"""
    path = Path(value)
    try:
        return _NATIVE_RESOLVE(path, strict=strict)
    except OSError as exc:
        if os.name != "nt" or getattr(exc, "winerror", None) != 1005:
            raise
        if ".." in path.parts:
            raise
        if not path.is_absolute():
            if path.drive:
                raise
            path = Path.cwd() / path
        if not path.is_absolute() or len(path.drive) != 2 or not path.drive.endswith(":"):
            raise
        root = Path(path.anchor)
        if not _cryptomator_volume(root):
            raise
        # realpathを諦める前に全ての既存要素をlstatする。
        # リンク/ジャンクション等の転送と、アクセス拒否・ロック済みドライブは通過させない。
        current = root
        parts = [
            root,
            *(root.joinpath(*path.parts[1:index]) for index in range(2, len(path.parts) + 1)),
        ]
        for current in parts:
            try:
                info = current.lstat()
            except FileNotFoundError:
                if current == root or strict:
                    raise
                break
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError(
                    "Cryptomator内のリンク/リパースポイントは使用できません：" + str(current)
                ) from exc
        return Path(os.path.abspath(path))


@contextmanager
def script_paths():
    """既存Wikiスクリプトを実行する子プロセス内だけで同じ互換処理を使う。"""
    original = Path.resolve
    Path.resolve = lambda self, strict=False: resolve_path(self, strict=strict)
    try:
        yield
    finally:
        Path.resolve = original


def main():
    script, *args = sys.argv[1:]
    sys.argv = [script, *args]
    sys.path.insert(0, str(Path(script).parent))
    with script_paths():
        runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    sys.modules["paperflow.paths"] = sys.modules[__name__]
    main()
