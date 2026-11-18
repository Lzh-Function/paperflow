import os
import stat
from pathlib import Path

from paperflow.models import PDFCandidate


def is_redirect(path: Path) -> bool:
    info = path.lstat()
    return path.is_symlink() or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def discover(root: Path, recurse: bool = True) -> list[PDFCandidate]:
    if not root.is_dir():
        raise FileNotFoundError(f"Paperpileが見つかりません: {root}")
    found = []

    def failure(error):
        raise error

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=failure):
        dirs[:] = [d for d in dirs if not is_redirect(Path(directory) / d)] if recurse else []
        for name in sorted(files):
            path = Path(directory) / name
            if path.suffix.lower() == ".pdf" and not is_redirect(path):
                info = path.stat()
                found.append(PDFCandidate(path, info.st_size, info.st_mtime_ns))
    return sorted(found, key=lambda p: str(p.path))
