import os
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIX = Path(__file__).parent / "fixtures"


def need(*binaries):
    missing = [b for b in binaries if not shutil.which(b)]
    return pytest.mark.skipif(bool(missing), reason=f"not installed: {missing}")


@pytest.fixture
def engine(tmp_path, monkeypatch):
    from edaport.server.engine import Engine
    monkeypatch.setenv("EDAPORT_IMPORT_ROOTS", str(ROOT))
    return Engine(tmp_path / "home", runner=os.environ.get("EDAPORT_RUNNER", "local"))


@pytest.fixture
def counter(engine):
    return engine.call("snapshot_import", path=str(ROOT / "examples/counter"), label="main")["design_ref"]
