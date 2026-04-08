from __future__ import annotations

import sys
from pathlib import Path
from typing import List

from fastapi import Depends, FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware

# Allow importing from project root when running: uvicorn main:app --reload
sys.path.append(str(Path(__file__).resolve().parents[1]))

from backend.auth import require_auth  # noqa: E402
from rules import (  # noqa: E402
    aggregate_results,
    compute_flags,
    evaluate_rules,
    merge_data,
)
from utils import read_csv_uploadfile  # noqa: E402
from schemas.user_schema import Summary, UploadResponse  # noqa: E402

app = FastAPI(title="UAR Copilot API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _summarize_findings(findings) -> Summary:
    counts = {"high": 0, "medium": 0, "low": 0}
    for f in findings:
        sev = str(getattr(f, "severity", "")).lower()
        if sev in counts:
            counts[sev] += 1
    return Summary(high=counts["high"], medium=counts["medium"], low=counts["low"], total=len(findings))


@app.post("/upload", response_model=UploadResponse)
async def upload_files(
    user_access: UploadFile = File(...),
    termination: UploadFile = File(...),
    _principal: dict = Depends(require_auth),
):
    user_access_df = read_csv_uploadfile(user_access)
    termination_df = read_csv_uploadfile(termination)
    df = merge_data(user_access_df, termination_df)
    df = compute_flags(df)
    results = evaluate_rules(df)
    findings = aggregate_results(results)
    return UploadResponse(summary=_summarize_findings(findings), findings=findings)


@app.get("/health")
def health():
    return {"status": "ok"}
