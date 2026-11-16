"""Evidence pack export (FR-21).

Builds a ZIP an auditor can work from without access to this system:

    README.txt               what each file is and how to verify the pack
    manifest.json            campaign facts, chain verification, SHA-256 of every file
    report.pdf               human-readable summary
    population/<n>_<file>    the uploaded files, byte-for-byte as received
    column_mappings.csv      how each file's columns were interpreted
    identities.csv           account -> person links, method, confidence, who confirmed
    findings.csv             rule ID + version, severity, inputs, explanation
    review_items.csv         every item with its final decision
    decisions.csv            full decision history, including changed decisions
    evidence_events.jsonl    the hash-chained event ledger

The export is itself recorded as an evidence event carrying the manifest's
SHA-256, which ties this exact pack to the campaign's ledger.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fpdf import FPDF
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.db.models import (
    Account,
    AccountEntitlement,
    Application,
    Campaign,
    Decision,
    Entitlement,
    EvidenceEvent,
    Finding,
    IdentityLink,
    Person,
    ReviewItem,
    SourceSnapshot,
)
from backend.domain.evidence import record_event, verify_chain
from backend.domain.identity import campaign_accounts
from backend.domain import remediation as remediation_service
from backend.domain.review import review_progress

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "none": 4}


@dataclass
class EvidencePack:
    filename: str
    content: bytes
    manifest_sha256: str


def _csv(rows: List[Dict[str, Any]], columns: List[str]) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in columns})
    return buf.getvalue().encode("utf-8")


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def _human(dt: Optional[datetime]) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if dt else "-"


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "file"


# ---------------------------------------------------------------------------
# Collect
# ---------------------------------------------------------------------------


def _collect(session: Session, campaign: Campaign) -> Dict[str, Any]:
    people = {p.id: p for p in session.scalars(select(Person))}
    apps = {a.id: a for a in session.scalars(select(Application))}
    entitlements = {e.id: e for e in session.scalars(select(Entitlement))}
    accounts = {a.id: a for a in campaign_accounts(session, campaign)}
    links = {link.account_id: link for link in session.scalars(
        select(IdentityLink).where(IdentityLink.account_id.in_(list(accounts))))}
    snapshots = list(session.scalars(
        select(SourceSnapshot).where(SourceSnapshot.campaign_id == campaign.id).order_by(SourceSnapshot.created_at)))
    findings = list(session.scalars(select(Finding).where(Finding.campaign_id == campaign.id)))
    items = list(session.scalars(select(ReviewItem).where(ReviewItem.campaign_id == campaign.id)))
    grants = {g.id: g for g in session.scalars(
        select(AccountEntitlement).where(AccountEntitlement.id.in_([i.account_entitlement_id for i in items])))}
    decisions = list(session.scalars(
        select(Decision).where(Decision.review_item_id.in_([i.id for i in items])).order_by(Decision.decided_at)))
    events = list(session.scalars(
        select(EvidenceEvent).where(EvidenceEvent.campaign_id == campaign.id).order_by(EvidenceEvent.seq)))

    def who(pid):
        p = people.get(pid) if pid else None
        return (p.name or p.email) if p else None

    def email(pid):
        p = people.get(pid) if pid else None
        return p.email if p else None

    identity_rows = []
    for a in sorted(accounts.values(), key=lambda a: (apps[a.application_id].name, a.native_id)):
        link = links.get(a.id)
        identity_rows.append({
            "application": apps[a.application_id].name, "account": a.native_id, "account_email": a.email,
            "account_type": a.account_type.value, "account_status": a.status,
            "status": link.status if link else "not_resolved",
            "person": who(link.person_id) if link else None, "person_email": email(link.person_id) if link else None,
            "method": link.method if link else None,
            "confidence": link.confidence if link else None,
            "confirmed_by": link.confirmed_by if link else None,
            "confirmed_at": _iso(link.confirmed_at) if link else None,
        })

    finding_rows = []
    for f in sorted(findings, key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.rule_id)):
        a = accounts.get(f.account_id)
        finding_rows.append({
            "finding_id": str(f.id), "rule_id": f.rule_id, "rule_version": f.rule_version, "severity": f.severity,
            "application": apps[a.application_id].name if a else None, "account": a.native_id if a else None,
            "entitlement": (f.inputs or {}).get("entitlement"),
            "inputs": json.dumps(f.inputs, sort_keys=True, default=str), "explanation": f.explanation,
        })

    latest: Dict[Any, Decision] = {}
    for d in decisions:
        latest[d.review_item_id] = d
    item_rows = []
    item_lookup = {}
    for i in items:
        g = grants[i.account_entitlement_id]
        a = accounts.get(g.account_id)
        link = links.get(g.account_id)
        d = latest.get(i.id)
        row = {
            "review_item_id": str(i.id), "severity": i.severity,
            "application": apps[a.application_id].name if a else None, "account": a.native_id if a else None,
            "entitlement": entitlements[g.entitlement_id].name,
            "account_type": a.account_type.value if a else None,
            "person": who(link.person_id) if link else None,
            "employment_status": people[link.person_id].employment_status if link and link.person_id in people else None,
            "reviewer": who(i.reviewer_id), "reviewer_email": email(i.reviewer_id),
            "finding_ids": ";".join(i.finding_ids or []),
            "decision": d.action.value if d else "pending", "justification": d.justification if d else None,
            "decided_by": d.actor_id if d else None, "decided_at": _iso(d.decided_at) if d else None,
            "decided_by_assigned_reviewer": d.as_assigned_reviewer if d else None,
            "override_reason": d.override_reason if d else None,
            # Claude's draft suggestion, if one was made, and whether the person's decision matched it.
            "ai_suggestion": i.recommendation.value if i.recommendation else None,
            "ai_confidence": (i.recommendation_meta or {}).get("confidence"),
            "ai_rationale": i.rationale if i.recommendation else None,
            "ai_model": (i.recommendation_meta or {}).get("model"),
            "ai_suggestion_followed": (d.action == i.recommendation) if (d and i.recommendation) else None,
        }
        item_rows.append(row)
        item_lookup[i.id] = row
    item_rows.sort(key=lambda r: (SEVERITY_ORDER.get(r["severity"], 9), r["application"] or "", r["account"] or ""))

    decision_rows = []
    for d in decisions:
        r = item_lookup.get(d.review_item_id, {})
        superseded = latest.get(d.review_item_id) is not d
        decision_rows.append({
            "decision_id": str(d.id), "review_item_id": str(d.review_item_id),
            "application": r.get("application"), "account": r.get("account"), "entitlement": r.get("entitlement"),
            "action": d.action.value, "justification": d.justification, "actor": d.actor_id,
            "decided_at": _iso(d.decided_at), "channel": d.channel, "superseded": superseded,
            "assigned_reviewer": who(d.assigned_reviewer_id), "assigned_reviewer_email": email(d.assigned_reviewer_id),
            "decided_by_assigned_reviewer": d.as_assigned_reviewer, "override_reason": d.override_reason,
        })

    mapping_rows = []
    for s in snapshots:
        for m in s.column_mapping or []:
            mapping_rows.append({"snapshot_id": str(s.id), "filename": s.filename,
                                 "application": apps[s.application_id].name if s.application_id in apps else None,
                                 **m, "confirmed_by": s.mapping_confirmed_by})

    remediation_rows = [{**r, "id": str(r["id"]), "resolved_at": _iso(r["resolved_at"]),
                         "verified_at": _iso(r["verified_at"]),
                         "verified_snapshot_id": str(r["verified_snapshot_id"]) if r["verified_snapshot_id"] else None}
                        for r in remediation_service.rows(session, campaign)]

    return {
        "remediation_rows": remediation_rows,
        "apps": apps, "snapshots": snapshots, "events": events, "identity_rows": identity_rows,
        "finding_rows": finding_rows, "item_rows": item_rows, "decision_rows": decision_rows,
        "mapping_rows": mapping_rows,
    }


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

_REPLACEMENTS = {"→": "->", "←": "<-", "–": "-", "—": "-", "“": '"', "”": '"', "’": "'", "‘": "'", "…": "...",
                 "✓": "OK", "✗": "X", "•": "-"}


def _latin1(text: Any) -> str:
    """Core PDF fonts are Latin-1; keep accented names, replace the rest."""
    s = "" if text is None else str(text)
    for k, v in _REPLACEMENTS.items():
        s = s.replace(k, v)
    return s.encode("latin-1", "replace").decode("latin-1")


class _Report(FPDF):
    def __init__(self, title: str):
        super().__init__(format="A4")
        self.title_text = title
        self.set_auto_page_break(auto=True, margin=16)
        self.set_margins(16, 16, 16)

    def header(self):
        self.set_font("Helvetica", "", 8)
        self.set_text_color(110, 106, 95)
        self.cell(0, 6, _latin1(f"UAR Copilot evidence pack - {self.title_text}"), align="L")
        self.ln(8)
        self.set_text_color(20, 20, 20)

    def footer(self):
        self.set_y(-12)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(110, 106, 95)
        self.cell(0, 6, f"Page {self.page_no()}/{{nb}}", align="R")

    def h1(self, text):
        self.set_font("Helvetica", "B", 18)
        self.multi_cell(0, 9, _latin1(text), new_x="LMARGIN", new_y="NEXT")
        self.ln(2)

    def h2(self, text):
        self.ln(3)
        self.set_font("Helvetica", "B", 12)
        self.multi_cell(0, 7, _latin1(text), new_x="LMARGIN", new_y="NEXT")
        self.ln(1)

    def para(self, text, size=9.5):
        self.set_font("Helvetica", "", size)
        self.multi_cell(0, 5, _latin1(text), new_x="LMARGIN", new_y="NEXT")
        self.ln(1)

    def kv(self, rows):
        self.set_font("Helvetica", "", 9.5)
        for k, v in rows:
            self.set_font("Helvetica", "B", 9.5)
            self.cell(55, 5.5, _latin1(k))
            self.set_font("Helvetica", "", 9.5)
            self.multi_cell(0, 5.5, _latin1(v), new_x="LMARGIN", new_y="NEXT")

    def table_ctx(self, headers, rows, widths):
        tbl = self.table(
            col_widths=widths, text_align="LEFT", line_height=4.6, first_row_as_headings=True,
            borders_layout="HORIZONTAL_LINES",
        )
        return _TableWriter(self, tbl, headers, rows)


class _TableWriter:
    def __init__(self, pdf, tbl, headers, rows):
        self.pdf, self.tbl, self.headers, self.rows = pdf, tbl, headers, rows

    def __enter__(self):
        t = self.tbl.__enter__()
        self.pdf.set_font("Helvetica", "B", 8.5)
        r = t.row()
        for h in self.headers:
            r.cell(_latin1(h))
        self.pdf.set_font("Helvetica", "", 8.5)
        for row in self.rows:
            r = t.row()
            for v in row:
                r.cell(_latin1(v))
        return t

    def __exit__(self, *exc):
        return self.tbl.__exit__(*exc)


def _pdf(campaign: Campaign, data: Dict[str, Any], chain: Dict[str, Any], progress: Dict[str, Any],
         generated_at: str, generated_by: str) -> bytes:
    pdf = _Report(campaign.name)
    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.h1(f"Access review evidence: {campaign.name}")
    signed = campaign.signed_off_at is not None
    if not signed:
        pdf.set_text_color(180, 35, 24)
        pdf.para("DRAFT - this campaign has not been signed off. Decisions may still change.", 10.5)
        pdf.set_text_color(20, 20, 20)
    pdf.kv([
        ("Review period", f"{campaign.period_start} to {campaign.period_end}"
                          if campaign.period_start and campaign.period_end else "Not set"),
        ("Campaign state", campaign.state.value.replace("_", " ")),
        ("Rule set version", campaign.rule_set_version or "-"),
        ("Signed off by", f"{campaign.signed_off_by}, {_human(campaign.signed_off_at)}" if signed else "Not signed off"),
        ("Generated", f"{_human(datetime.fromisoformat(generated_at))} by {generated_by}"),
        ("Evidence chain", (f"Verified: {chain['events']} events, head {chain.get('head_hash', '')[:16]}..."
                            if chain["valid"] else f"BROKEN at event {chain.get('broken_at_seq')}")),
    ])

    pdf.h2("1. Population (source files)")
    pdf.para("Each file is stored byte-for-byte in the population/ folder of this pack. Recompute its SHA-256 to "
             "confirm it is the file that was reviewed.")
    with pdf.table_ctx(["File", "Kind / application", "Rows", "SHA-256"],
                       [[s.filename, "HR roster" if s.kind.value == "hr_roster" else
                         ("Verification: " + (data["apps"][s.application_id].name if s.application_id in data["apps"]
                                              else "")) if s.kind.value == "verification" else
                         (data["apps"][s.application_id].name if s.application_id in data["apps"] else "access list"),
                         s.row_count, s.sha256] for s in data["snapshots"]], (45, 35, 12, 86)):
        pass

    pdf.h2("2. Identity resolution")
    ir = data["identity_rows"]
    by = {}
    for r in ir:
        by[r["status"]] = by.get(r["status"], 0) + 1
    manual = sum(1 for r in ir if r["method"] == "manual")
    pdf.para(f"{len(ir)} accounts: " + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in sorted(by.items()))
             + f". {manual} decided by a person; details in identities.csv.")

    pdf.h2("3. Findings")
    fr = data["finding_rows"]
    sev = {}
    for r in fr:
        sev[r["severity"]] = sev.get(r["severity"], 0) + 1
    pdf.para(f"{len(fr)} findings from the deterministic rules engine (no AI involved): "
             + ", ".join(f"{sev.get(s, 0)} {s}" for s in ("critical", "high", "medium", "low"))
             + ". Each lists its rule ID, version and exact inputs in findings.csv so it can be re-performed.")
    top = [r for r in fr if r["severity"] in ("critical", "high")]
    if top:
        with pdf.table_ctx(["Severity", "Rule", "Application / account", "Entitlement"],
                           [[r["severity"], f"{r['rule_id']} v{r['rule_version']}",
                             f"{r['application']} / {r['account']}", r["entitlement"] or "(account)"] for r in top],
                           (20, 62, 56, 40)):
            pass

    pdf.h2("4. Review decisions")
    overrides = sum(1 for r in data["item_rows"] if r["decided_by_assigned_reviewer"] is False)
    pdf.para(f"{progress['decided']} of {progress['total']} items decided: {progress['by_action']['keep']} kept, "
             f"{progress['by_action']['revoke']} revoked, {progress['by_action']['modify']} to modify. "
             f"{progress['decided'] - overrides} decided by the assigned reviewer; {overrides} decided by the campaign "
             "owner instead, each with a recorded override reason. Every decision, including changed ones, is in "
             "decisions.csv.")
    risky = [r for r in data["item_rows"] if r["severity"] in ("critical", "high", "medium")]
    if risky:
        with pdf.table_ctx(["Sev.", "Access", "Person", "Decision", "By / justification"],
                           [[r["severity"], f"{r['application']} {r['entitlement']} ({r['account']})",
                             r["person"] or r["account_type"].replace("_", " ").replace("non human", "service/shared"),
                             r["decision"],
                             f"{r['decided_by'] or '-'}"
                             + (" (assigned reviewer)" if r["decided_by_assigned_reviewer"] else
                                f" (owner override: {r['override_reason']})" if r["override_reason"] else "")
                             + (f". Reason: {r['justification']}" if r["justification"] else "")]
                            for r in risky], (15, 52, 30, 20, 61)):
            pass

    rem = data["remediation_rows"]
    pdf.h2("5. Remediation")
    if not rem:
        pdf.para("No access changes were required." if campaign.signed_off_at else
                 "Remediation starts when the campaign is signed off.")
    else:
        done = sum(1 for r in rem if r["status"] in ("verified", "accepted"))
        pdf.para(f"{len(rem)} access change(s) from revoke/modify decisions; {done} confirmed. 'verified' means a later "
                 "upload or connector pull of that application's access list no longer shows the access (it is in "
                 "population/); "
                 "'accepted' means it was closed with a written reason instead. Details in remediations.csv.")
        with pdf.table_ctx(["Change", "Access", "Status", "Ticket / evidence"],
                           [[r["action"], f"{r['application']} {r['entitlement']} ({r['account']})", r["status"],
                             " ".join(x for x in [r["ticket_ref"] or "",
                                                  f"verified by {'re-pull' if r['verification_method'] == 'repull' else 'upload'} {r['verified_at'][:10]}" if r["verification_method"] in ("reupload", "repull") and r["verified_at"] else "",
                                                  f"reason: {r['note']}" if r["status"] == "accepted" and r["note"] else ""] if x) or "-"]
                            for r in rem], (18, 70, 22, 68)):
            pass

    pdf.h2("6. How to verify this pack")
    pdf.para("1. manifest.json lists the SHA-256 of every other file in this pack; recompute and compare. "
             "2. evidence_events.jsonl is a hash chain: each event's hash is SHA-256 of the previous hash followed "
             "by the event's canonical JSON (keys sorted, no spaces; fields campaign_id, seq, type, payload, actor, "
             "actor_kind, occurred_at). The first event's previous hash is 64 zeros. "
             "3. Findings can be re-performed from the inputs recorded in findings.csv and the population files.")
    return bytes(pdf.output())


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


README = """UAR Copilot evidence pack
=========================

Campaign: {name}
Generated: {generated_at} by {generated_by}
Status: {status}

Files
-----
report.pdf              Human-readable summary of the review.
manifest.json           Campaign facts, evidence-chain verification and the SHA-256 of every file below.
population/             The uploaded source files, byte-for-byte as received.
column_mappings.csv     How each file's columns were interpreted, with confidence and who confirmed.
identities.csv          Which person each account belongs to, how that was decided and by whom.
findings.csv            Every finding: rule ID and version, severity, the exact inputs, and why it fired.
review_items.csv        Every reviewed entitlement with its reviewer and final decision. ai_* columns show
                        Claude's drafted suggestion when one was requested (advisory only; a person made
                        every decision) and whether the decision matched it. What was sent to Claude and
                        what came back are in the recommendation.generated events.
decisions.csv           Full decision history, including decisions that were later changed (superseded=True),
                        who the item was assigned to, and the override reason when someone else decided.
remediations.csv        Each access change from a revoke/modify decision and how it was confirmed.
evidence_events.jsonl   The append-only, hash-chained event ledger for the campaign.

Verifying
---------
1. Recompute SHA-256 for each file and compare with manifest.json ("files").
2. Recompute the chain: hash_n = SHA-256(hash_(n-1) + canonical JSON of event n), where the canonical
   JSON has keys sorted and no spaces, over: campaign_id, seq, type, payload, actor, actor_kind,
   occurred_at. hash_0 is 64 zeros. The last hash must equal manifest.json "evidence_chain.head_hash".
3. This export is recorded in the ledger as an "evidence.exported" event that carries the SHA-256 of
   manifest.json, so the pack can be matched to the system's record.
"""


def build_evidence_pack(session: Session, campaign: Campaign, actor: str) -> EvidencePack:
    data = _collect(session, campaign)
    chain = verify_chain(session, campaign.id)
    progress = review_progress(session, campaign)
    generated_at = datetime.now(timezone.utc).isoformat()

    files: Dict[str, bytes] = {}
    for n, s in enumerate(data["snapshots"], start=1):
        raw = s.content or b""
        if hashlib.sha256(raw).hexdigest() != s.sha256:
            raise ValueError(f"stored file {s.filename} no longer matches its recorded SHA-256")
        files[f"population/{n:02d}_{_safe_name(s.filename or s.kind.value)}"] = raw
    files["column_mappings.csv"] = _csv(data["mapping_rows"], [
        "filename", "application", "target", "matched_column", "method", "confidence", "needs_confirmation",
        "confirmed_by", "snapshot_id"])
    files["identities.csv"] = _csv(data["identity_rows"], [
        "application", "account", "account_email", "account_type", "account_status", "status", "person",
        "person_email", "method", "confidence", "confirmed_by", "confirmed_at"])
    files["findings.csv"] = _csv(data["finding_rows"], [
        "finding_id", "severity", "rule_id", "rule_version", "application", "account", "entitlement", "inputs",
        "explanation"])
    files["review_items.csv"] = _csv(data["item_rows"], [
        "review_item_id", "severity", "application", "account", "entitlement", "account_type", "person",
        "employment_status",
        "reviewer", "reviewer_email", "decision", "justification", "decided_by", "decided_at",
        "decided_by_assigned_reviewer", "override_reason", "finding_ids", "ai_suggestion", "ai_confidence",
        "ai_rationale", "ai_model", "ai_suggestion_followed"])
    files["decisions.csv"] = _csv(data["decision_rows"], [
        "decision_id", "review_item_id", "application", "account", "entitlement", "action", "justification",
        "actor", "decided_at", "channel", "superseded", "assigned_reviewer", "assigned_reviewer_email",
        "decided_by_assigned_reviewer", "override_reason"])
    files["remediations.csv"] = _csv(data["remediation_rows"], [
        "id", "status", "action", "severity", "application", "account", "entitlement", "person", "change",
        "decided_by", "ticket_ref", "note", "resolved_by", "resolved_at", "verification_method", "verified_at",
        "verified_snapshot_id"])
    files["evidence_events.jsonl"] = "".join(
        json.dumps({"seq": e.seq, "type": e.type, "actor": e.actor, "actor_kind": e.actor_kind,
                    "occurred_at": _iso(e.occurred_at), "campaign_id": str(e.campaign_id), "payload": e.payload,
                    "prev_hash": e.prev_hash, "hash": e.hash}, sort_keys=True, default=str) + "\n"
        for e in data["events"]).encode("utf-8")
    files["report.pdf"] = _pdf(campaign, data, chain, progress, generated_at, actor)
    status = (f"Signed off by {campaign.signed_off_by} at {_iso(campaign.signed_off_at)}"
              if campaign.signed_off_at else "DRAFT - not signed off")
    files["README.txt"] = README.format(name=campaign.name, generated_at=generated_at, generated_by=actor,
                                        status=status).encode("utf-8")

    manifest = {
        "format": "uar-copilot-evidence-pack/1",
        "campaign": {
            "id": str(campaign.id), "name": campaign.name, "state": campaign.state.value,
            "period_start": str(campaign.period_start) if campaign.period_start else None,
            "period_end": str(campaign.period_end) if campaign.period_end else None,
            "rule_set_version": campaign.rule_set_version,
            "signed_off_by": campaign.signed_off_by, "signed_off_at": _iso(campaign.signed_off_at),
        },
        "generated_at": generated_at,
        "generated_by": actor,
        "draft": campaign.signed_off_at is None,
        "review_progress": {k: progress[k] for k in ("total", "decided", "pending", "by_action")},
        "evidence_chain": chain,
        "files": {name: {"sha256": hashlib.sha256(b).hexdigest(), "bytes": len(b)} for name, b in sorted(files.items())},
    }
    manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", manifest_bytes)
        for name, b in sorted(files.items()):
            zf.writestr(name, b)

    record_event(session, campaign, "evidence.exported",
                 {"manifest_sha256": manifest_sha, "files": len(files) + 1, "draft": manifest["draft"],
                  "chain_head_at_export": chain.get("head_hash")}, actor)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    return EvidencePack(f"evidence-{_safe_name(campaign.name)}-{stamp}.zip", buf.getvalue(), manifest_sha)
