from fastapi import FastAPI, UploadFile, File, HTTPException, Header, Form
from typing import Optional
from datetime import datetime, date, timezone
from pathlib import Path
import tempfile
import os
import importlib.util
import secrets
import uuid
import json
import csv
import httpx

ENGINE = Path(__file__).parent / "leakguard_engine.py"
spec = importlib.util.spec_from_file_location("leakguard_engine", ENGINE)
mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(mod)

BACKEND_VERSION = "0.2"
ENGINE_VERSION = "0.1"
API_KEY = os.getenv("API_KEY", "").strip()
MAX_MB = int(os.getenv("MAX_UPLOAD_MB", "25"))
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

app = FastAPI(title="LeakGuard Finance Engine", version=BACKEND_VERSION)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def require_api_key(x_api_key: Optional[str]) -> None:
    # Health is intentionally public. All data operations require an API key.
    if not API_KEY:
        raise HTTPException(503, "API authentication is not configured")
    if not x_api_key or not secrets.compare_digest(x_api_key, API_KEY):
        raise HTTPException(401, "Invalid API key")


def require_supabase() -> None:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(503, "Database persistence is not configured")


def supabase_headers(prefer: str = "return=minimal") -> dict:
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        "Prefer": prefer,
    }


async def supabase_request(method: str, table: str, *, json_body=None, params=None):
    require_supabase()
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    timeout = httpx.Timeout(30.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.request(
            method,
            url,
            headers=supabase_headers("return=representation" if method == "GET" else "return=minimal"),
            json=json_body,
            params=params,
        )
    if response.status_code >= 400:
        detail = response.text[:1000]
        raise RuntimeError(f"Supabase {method} {table} failed ({response.status_code}): {detail}")
    if not response.content:
        return None
    try:
        return response.json()
    except Exception:
        return None


async def insert_analysis_run(run: dict, company_id: Optional[str]) -> None:
    row = {
        "analysis_run_id": run["analysis_run_id"],
        "company_id": company_id,
        "started_at": run["started_at"],
        "completed_at": run["completed_at"],
        "status": "Completed",
        "records_received": run.get("records_received", 0),
        "records_processed": run.get("records_processed", 0),
        "records_rejected": run.get("records_rejected", 0),
        "candidate_count": run.get("candidate_count", 0),
        "high_confidence_count": run.get("high_confidence_count", 0),
        "potential_recovery": run.get("potential_recovery_total", 0),
        "validated_recovery": run.get("validated_recovery_total", 0),
        "actual_recovered": run.get("actual_recovered_total", 0),
    }
    await supabase_request("POST", "analysis_runs", json_body=row)


def finding_to_row(f: dict, analysis_run_id: str) -> dict:
    return {
        "finding_id": f["finding_id"],
        "analysis_run_id": analysis_run_id,
        "finding_type": f.get("finding_type", "Unknown"),
        "confidence": f.get("confidence", "Low"),
        "status": f.get("status", "New"),
        "supplier_customer": f.get("supplier_or_customer"),
        "invoice_numbers": f.get("invoice_numbers", []),
        "dates": f.get("dates", []),
        "amount": f.get("amount", 0),
        "currency": f.get("currency"),
        "potential_recovery": f.get("potential_recovery", 0),
        "validated_recovery": f.get("validated_recovery", 0),
        "actual_recovered": f.get("actual_recovered", 0),
        "evidence": f.get("evidence"),
        "rule_triggered": f.get("rule_triggered"),
        "explanation": f.get("explanation"),
        "recommended_action": f.get("recommended_action"),
        "source_rows": f.get("source_rows", []),
        "review_notes": f.get("review_notes"),
        "owner": f.get("owner"),
    }


async def insert_findings(findings: list[dict], analysis_run_id: str) -> None:
    if not findings:
        return
    rows = [finding_to_row(f, analysis_run_id) for f in findings]
    await supabase_request("POST", "findings", json_body=rows)


async def insert_audit(analysis_run_id: Optional[str], event: str, details: dict) -> None:
    row = {
        "analysis_run_id": analysis_run_id,
        "event": event,
        "details": details,
    }
    await supabase_request("POST", "audit_log", json_body=row)


async def mark_run_failed(analysis_run_id: str, message: str) -> None:
    try:
        await supabase_request(
            "PATCH",
            "analysis_runs",
            params={"analysis_run_id": f"eq.{analysis_run_id}"},
            json_body={"status": "Failed", "completed_at": utc_now_iso()},
        )
    except Exception:
        pass
    try:
        await insert_audit(analysis_run_id, "analysis_persistence_failed", {"error": message[:1000]})
    except Exception:
        pass


async def save_run(run: dict, company_id: Optional[str]) -> None:
    await insert_analysis_run(run, company_id)
    try:
        await insert_findings(run.get("findings", []), run["analysis_run_id"])
        await insert_audit(
            run["analysis_run_id"],
            "analysis_completed",
            {
                "records_received": run.get("records_received", 0),
                "candidate_count": run.get("candidate_count", 0),
                "potential_recovery_total": run.get("potential_recovery_total", 0),
                "high_confidence_count": run.get("high_confidence_count", 0),
            },
        )
    except Exception as exc:
        await mark_run_failed(run["analysis_run_id"], str(exc))
        raise


async def read_upload_to_temp(upload: UploadFile, label: str) -> str:
    filename = upload.filename or ""
    if Path(filename).suffix.lower() != ".csv":
        raise HTTPException(400, f"{label.upper()} file must be CSV")
    if len(filename) > 255:
        raise HTTPException(400, f"{label.upper()} filename is too long")

    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    total = 0
    try:
        with open(path, "wb") as target:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_MB * 1024 * 1024:
                    raise HTTPException(413, "File exceeds configured size limit")
                target.write(chunk)
        if total == 0:
            raise HTTPException(400, f"{label.upper()} file is empty")
        return path
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        raise


def validate_analysis_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        date.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, "analysis_date must be YYYY-MM-DD")
    return value


@app.get("/health")
def health():
    return {
        "status": "ok",
        "backend_version": BACKEND_VERSION,
        "engine_version": ENGINE_VERSION,
        "tests": ["duplicate_payments", "supplier_credits", "overdue_ar"],
        "api_auth_configured": bool(API_KEY),
        "persistence_configured": bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY),
    }


@app.post("/analyze")
async def analyze(
    ap_file: Optional[UploadFile] = File(None),
    ar_file: Optional[UploadFile] = File(None),
    analysis_date: Optional[str] = Form(None),
    company_id: Optional[str] = Form(None),
    x_api_key: Optional[str] = Header(None),
):
    require_api_key(x_api_key)
    require_supabase()
    if not ap_file and not ar_file:
        raise HTTPException(400, "Provide ap_file and/or ar_file")

    analysis_date = validate_analysis_date(analysis_date)
    if company_id:
        try:
            uuid.UUID(company_id)
        except ValueError:
            raise HTTPException(400, "company_id must be a valid UUID")

    paths = []
    run_id = None
    try:
        kwargs = {}
        for label, upload in [("ap", ap_file), ("ar", ar_file)]:
            if upload:
                path = await read_upload_to_temp(upload, label)
                paths.append(path)
                kwargs[label + "_csv"] = path

        run = mod.run(analysis_date=analysis_date, **kwargs)
        run_id = run.get("analysis_run_id")
        if not run_id:
            raise RuntimeError("Engine did not return analysis_run_id")

        await save_run(run, company_id)
        # Do not return raw uploaded data; only structured findings/evidence.
        return run
    except HTTPException:
        raise
    except Exception as exc:
        if run_id:
            await mark_run_failed(run_id, str(exc))
        raise HTTPException(500, "Analysis failed. Check the analysis run audit log.")
    finally:
        for p in paths:
            try:
                os.remove(p)
            except OSError:
                pass


@app.get("/analysis/{analysis_run_id}")
async def get_analysis(analysis_run_id: str, x_api_key: Optional[str] = Header(None)):
    require_api_key(x_api_key)
    try:
        rows = await supabase_request(
            "GET",
            "analysis_runs",
            params={"analysis_run_id": f"eq.{analysis_run_id}", "limit": "1"},
        )
        if not rows:
            raise HTTPException(404, "Analysis run not found")
        return rows[0]
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(500, "Unable to retrieve analysis run")


@app.get("/analysis/{analysis_run_id}/findings")
async def get_findings(analysis_run_id: str, x_api_key: Optional[str] = Header(None)):
    require_api_key(x_api_key)
    try:
        rows = await supabase_request(
            "GET",
            "findings",
            params={"analysis_run_id": f"eq.{analysis_run_id}", "order": "created_at.asc"},
        )
        return {"analysis_run_id": analysis_run_id, "count": len(rows or []), "findings": rows or []}
    except Exception:
        raise HTTPException(500, "Unable to retrieve findings")
