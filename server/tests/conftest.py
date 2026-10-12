import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(Path(__file__).resolve().parent))

OUTPUT = Path(__file__).resolve().parent / "output"


@pytest.fixture
def output_dir() -> Path:
    OUTPUT.mkdir(exist_ok=True)
    return OUTPUT
