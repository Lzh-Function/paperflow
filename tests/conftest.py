import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from create_demo import make_demo

from paperflow.config import load_config


@pytest.fixture
def config(tmp_path):
    return load_config(make_demo(tmp_path / "fixture"))


@pytest.fixture
def small_config(tmp_path):
    return load_config(make_demo(tmp_path / "fixture", count=1))


def tree(root):
    return {
        str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in root.rglob("*")
        if p.is_file()
    }
