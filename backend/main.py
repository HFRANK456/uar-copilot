from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from fastapi import Depends, FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware

# Allow `uvicorn main:app` from inside backend/ as well as `uvicorn backend.main:app` from the root.
sys.path.append(str(Path(__file__).resolve().parents[1]))

from backend.auth import auth_disabled, require_auth  # noqa: E402
from backend.rule_registry import RULE_SET_VERSION  # noqa: E402
from backend.rules import aggregate_results, compute_flags, evaluate_rules, merge_data  # noqa: E402
from backend.utils import read_csv_uploadfile  # noqa: E402
from schemas.user_schema import ColumnMapping, InputDigest, Summary, UploadResponse  # noqa: E402


class _JsonFormatter(logging.Formatter):
    _std = set(vars(logging.LogRecord("", 0, "", 0, "", None, None)))

    def format(self, record: logging.LogRecord) -> str:
        payload = {"level": record.levelname, "logger": record.name, "event": record.getMessage()}
        payload.update({k: v for k, v in vars(record).items() if k not in self._std and k != "message"})
        return json.dumps(payload, default=str)


_handler = logging.StreamHandler()
_handler.setFormatter(_JsonFormatter())
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), handlers=[_handler], force=True)
logger = logging.getLogger("uar")

DEFAULT_CORS_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"
cors_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", DEFAULT_CORS_ORIGINS).split(",") if o.strip() and o.strip() != "*"]

app = FastAPI(title="UAR Copilot API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=False,  # bearer tokens, not cookies
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)

if auth_disabled():
    logger.warning("auth_disabled", extra={"note": "AUTH_DISABLED is set; never use in a deployed environment"})


def _summarize_findings(findings) -> Summary:
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for f in findings:
        sev = str(getattr(f, "severity", "")).lower()
        if sev in counts:
            counts[sev] += 1
    return Summary(**counts, total=len(findings))


@app.post("/upload", response_model=UploadResponse)
async def upload_files(
    user_access: UploadFile = File(...),
    termination: UploadFile = File(...),
    principal: dict = Depends(require_auth),
):
    user_access_df, user_sha = read_csv_uploadfile(user_access)
    termination_df, term_sha = read_csv_uploadfile(termination)

    mappings: List[ColumnMapping] = []
    df = merge_data(user_access_df, termination_df, mappings)
    df = compute_flags(df)
    findings = aggregate_results(evaluate_rules(df))

    logger.info(
        "review_run",
        extra={
            "actor": principal.get("sub"),
            "user_access_sha256": user_sha,
            "termination_sha256": term_sha,
            "rows": len(user_access_df),
            "findings": len(findings),
            "rule_set_version": RULE_SET_VERSION,
        },
    )

    return UploadResponse(
        summary=_summarize_findings(findings),
        findings=findings,
        column_mappings=mappings,
        inputs=[
            InputDigest(source="user_access", filename=user_access.filename, sha256=user_sha, row_count=len(user_access_df)),
            InputDigest(source="termination", filename=termination.filename, sha256=term_sha, row_count=len(termination_df)),
        ],
        rule_set_version=RULE_SET_VERSION,
        evaluated_at=datetime.now(timezone.utc).isoformat(),
    )


@app.get("/health")
def health():
    return {"status": "ok"}
