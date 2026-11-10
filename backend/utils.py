from __future__ import annotations

import hashlib
import io
from typing import Iterable, Tuple

import pandas as pd
from fastapi import HTTPException, UploadFile

MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def read_csv_uploadfile(upload: UploadFile) -> Tuple[pd.DataFrame, str]:
    """Parse an uploaded CSV and return it with the SHA-256 of the raw bytes."""
    raw = upload.file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large: {upload.filename}")
    digest = hashlib.sha256(raw).hexdigest()
    try:
        df = pd.read_csv(io.BytesIO(raw))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid CSV file: {upload.filename}") from exc
    df.columns = [str(c).strip().lower() for c in df.columns]
    return df, digest


def require_columns(df: pd.DataFrame, columns: Iterable[str], label: str) -> None:
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Missing columns in {label}: {', '.join(missing)}",
        )
