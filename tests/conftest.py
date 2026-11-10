from pathlib import Path

import pandas as pd
import pytest

DATA = Path(__file__).resolve().parents[1] / "data"
AS_OF = pd.Timestamp("2026-11-10")


@pytest.fixture
def data_dir() -> Path:
    return DATA


def load(name: str) -> pd.DataFrame:
    return pd.read_csv(DATA / name)
