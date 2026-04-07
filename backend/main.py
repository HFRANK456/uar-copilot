from __future__ import annotations

import sys
from pathlib import Path
from typing import List

from fastapi import FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware

# Allow importing from project root when running: uvicorn main:app --reload
sys.path.append(str(Path(__file__).resolve().parents[1]))

from rules import (  # noqa: E402
    aggregate_results,
    compute_flags,
    evaluate_rules,
    merge_data,
)
from utils import read_csv_uploadfile  # noqa: E402
from schemas.user_schema import AggregatedUser  # noqa: E402

app = FastAPI(title="UAR Copilot API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/upload", response_model=List[AggregatedUser])
async def upload_files(
    user_access: UploadFile = File(...),
    termination: UploadFile = File(...),
):
    user_access_df = read_csv_uploadfile(user_access)
    termination_df = read_csv_uploadfile(termination)
    df = merge_data(user_access_df, termination_df)
    df = compute_flags(df)
    results = evaluate_rules(df)
    aggregated = aggregate_results(results)
    return aggregated


@app.get("/health")
def health():
    return {"status": "ok"}
