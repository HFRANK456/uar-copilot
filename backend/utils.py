from __future__ import annotations

from typing import Iterable

import pandas as pd
from fastapi import HTTPException, UploadFile


def read_csv_uploadfile(upload: UploadFile) -> pd.DataFrame:
    try:
        df = pd.read_csv(upload.file)
    except Exception as exc:  # pragma: no cover - defensive
        raise HTTPException(status_code=400, detail=f"Invalid CSV file: {upload.filename}") from exc
    df.columns = [str(c).strip().lower() for c in df.columns]
    return df


def require_columns(df: pd.DataFrame, columns: Iterable[str], label: str) -> None:
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Missing columns in {label}: {', '.join(missing)}",
        )


def is_active(value: object) -> bool:
    if value is None:
        return False
    text = str(value).strip().lower()
    return text in {"active", "enabled", "true", "1", "yes"}


def is_admin_role(role: object) -> bool:
    if role is None:
        return False
    text = str(role).strip().lower()
    return "admin" in text
