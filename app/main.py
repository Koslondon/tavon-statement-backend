"""
Tavon Partners — Merchant Statement Checker backend.

Design principles (per Kos):
  - Nothing is ever persisted. The uploaded PDF exists only for the duration
    of one request, in a temp file that is deleted in a `finally` block no
    matter how the request ends (success, parse failure, or crash).
  - No statement content is ever logged. Error logs may say "extraction
    failed" or "no processor matched" - never the extracted text itself.
  - Hard 5MB upload cap, enforced before the file is even fully read into
    memory, to keep this cheap to run and resistant to abuse.
"""
import os
import re
import subprocess
import tempfile
import logging

import resend
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field

from app.parsers import aib, clover, clover_fees, elavon, global_payments, intercard, dojo, trust_payments as trustpay

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("statement-checker")

MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5MB hard cap

# Per Kos: the client's own copy of their figures is sent via Resend
# (an account Tavon already has). Requires RESEND_API_KEY to be set as
# an environment variable on Render, and a "from" address on a domain
# verified in the Resend dashboard - until both of those are in place,
# every call to /email-report will fail with a clear 502, not silently.
resend.api_key = os.environ.get("RESEND_API_KEY", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "Tavon Partners <statements@tavonpartners.com>")

app = FastAPI(title="Tavon Partners Statement Checker")

# Locked to the real Tavon Partners domain (both apex and www, since
# either can be what the browser sends as Origin depending on how a
# visitor reaches the site).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://tavonpartners.com", "https://www.tavonpartners.com"],
    allow_methods=["POST"],
    allow_headers=["*"],
)


def extract_text(pdf_path: str) -> str:
    """Run pdftotext -layout against the file, exactly as every parser was
    validated against. Raises if poppler isn't available or the PDF has no
    text layer (scanned statements aren't supported by this endpoint)."""
    result = subprocess.run(
        ["pdftotext", "-layout", pdf_path, "-"],
        capture_output=True, text=True, timeout=20,
    )
    if result.returncode != 0:
        raise RuntimeError("pdftotext extraction failed")
    return result.stdout


# Ordered: check most-specific signatures first to avoid false positives.
PROCESSOR_SIGNATURES = [
    ("aib", re.compile(r"AIB Merchant Services")),
    ("intercard", re.compile(r"InterCard AG")),
    ("clover", re.compile(r"Fiserv, Clover and First Data")),
    ("elavon", re.compile(r"Elavon|U\.S\. Bank Europe")),
    ("global_payments", re.compile(r"Global Payments|GPUK")),
    ("dojo", re.compile(r"Paymentsense Limited")),
    ("trust_payments", re.compile(r"trustpayments\.com|support@trustpayments")),
]


def detect_processor(text: str) -> str | None:
    for name, pattern in PROCESSOR_SIGNATURES:
        if pattern.search(text):
            return name
    return None


def run_parser(processor: str, text: str) -> dict:
    lines = text.splitlines()

    if processor == "aib":
        items, stated = aib.parse_msc_table(lines)
        computed = sum(i["total_charge"] for i in items)
        return {"processor": "AIB Merchant Services", "rows": items,
                "computed_total": round(computed, 2), "stated_total": stated}

    if processor == "intercard":
        items, stated = intercard.parse_intercard_statement(lines)
        computed = sum(i["price"] for i in items) if items else 0
        return {"processor": "InterCard AG", "rows": items,
                "computed_total": round(computed, 2), "stated_total": stated}

    if processor == "clover":
        charge_items, charge_stated = clover.parse_service_charges(lines)
        fee_items, fee_stated = clover_fees.parse_fees(lines)
        return {
            "processor": "Clover / First Data",
            "service_charges": charge_items, "service_charges_stated": charge_stated,
            "fees": fee_items, "fees_stated": fee_stated,
        }

    if processor == "elavon":
        items, stated = elavon.parse_card_fees(lines)
        computed = sum(i["fee"] for i in items)
        # Card Fees alone (parsed above) is only the discount-rate portion
        # of the real cost - Activity Fees (per-transaction authorisation
        # charge) and the PCI/Other Fees line sit alongside it and are not
        # reflected in the Card Fees total. parse_summary() reads the
        # statement's own Fees Summary box, which already adds these
        # together into "Total Fees" - that combined figure (not the Card
        # Fees table total) is what should drive the blended rate.
        summary = elavon.parse_summary(lines)
        turnover = summary.get("turnover")
        true_total_fees = summary.get("total_fees", round(computed, 2))
        true_blended_rate = (
            round(true_total_fees / turnover * 100, 4) if turnover else None
        )
        return {
            "processor": "Elavon", "rows": items,
            "computed_total": round(computed, 2), "stated_total": stated,
            "turnover": turnover,
            "transaction_count": summary.get("transaction_count"),
            "activity_fees": summary.get("activity_fees"),
            "other_fees": summary.get("other_fees"),
            "true_total_fees": true_total_fees,
            "true_blended_rate_pct": true_blended_rate,
        }

    if processor == "global_payments":
        result = global_payments.parse_full_statement(text)
        return {"processor": "Global Payments", **result}

    if processor == "dojo":
        result = dojo.parse_statement(text)
        return {"processor": "Dojo", **result}

    if processor == "trust_payments":
        result = trustpay.parse_statement(text)
        return {"processor": "Trust Payments", **result}

    raise ValueError(f"no dispatcher wired for processor '{processor}'")


@app.post("/analyze")
async def analyze_statement(file: UploadFile = File(...)):
    if file.content_type != "application/pdf":
        raise HTTPException(400, "Please upload a PDF statement.")

    body = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(body) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File too large - 5MB maximum.")

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(body)
            tmp_path = tmp.name

        text = extract_text(tmp_path)
        processor = detect_processor(text)

        if processor is None:
            log.info("No processor signature matched for this upload.")
            return {
                "matched": False,
                "message": (
                    "We couldn't automatically recognise this statement format yet. "
                    "Share it with a Tavon representative directly and we'll take a look."
                ),
            }

        result = run_parser(processor, text)
        result["matched"] = True
        return result

    except subprocess.TimeoutExpired:
        log.warning("pdftotext timed out on an upload.")
        raise HTTPException(422, "Could not read this PDF in time.")
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see note below
        # Never log `exc` args that might embed extracted statement text -
        # just the exception type/class name is safe to record.
        log.warning("Statement processing failed: %s", type(exc).__name__)
        raise HTTPException(422, "Could not process this statement.")
    finally:
        # This is the whole point: nothing survives the request.
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


@app.get("/health")
async def health():
    return {"status": "ok"}


class EmailReportRequest(BaseModel):
    email: EmailStr
    provider: str = "—"
    blended_rate: str = "—"
    total_fees: str = "—"
    turnover: str = "—"
    transaction_count: str = "—"
    # Cap length defensively - this is free text built client-side from the
    # fee table, not something that should ever need to be huge.
    fee_breakdown: str = Field(default="", max_length=20000)


@app.post("/email-report")
async def email_report(req: EmailReportRequest):
    if not resend.api_key:
        # Fails loudly rather than pretending to send - RESEND_API_KEY
        # must be set as an env var on Render for this to work at all.
        log.error("email-report called but RESEND_API_KEY is not configured.")
        raise HTTPException(500, "Email delivery is not configured yet - please contact Tavon Partners directly.")

    breakdown_html = "".join(
        f"<tr><td style='padding:4px 10px 4px 0;white-space:pre'>{line}</td></tr>"
        for line in req.fee_breakdown.splitlines()
    ) or "<tr><td>No fee breakdown available.</td></tr>"

    html_body = f"""
    <div style="font-family:Arial,sans-serif;color:#111;max-width:560px">
      <h2 style="margin:0 0 12px">Your statement breakdown — {req.provider}</h2>
      <table style="width:100%;border-collapse:collapse;margin-bottom:16px">
        <tr><td style="padding:4px 10px 4px 0;color:#555">Gross card turnover</td><td>{req.turnover}</td></tr>
        <tr><td style="padding:4px 10px 4px 0;color:#555">Total fees charged</td><td>{req.total_fees}</td></tr>
        <tr><td style="padding:4px 10px 4px 0;color:#555">Blended rate</td><td>{req.blended_rate}</td></tr>
        <tr><td style="padding:4px 10px 4px 0;color:#555">Transactions processed</td><td>{req.transaction_count}</td></tr>
      </table>
      <h3 style="margin:0 0 8px">Fee breakdown</h3>
      <table style="width:100%;border-collapse:collapse;font-size:13px">{breakdown_html}</table>
      <p style="margin-top:20px;font-size:12px;color:#777">
        Sent at your request from the Tavon Partners Merchant Statement Checker.
        This information was not stored on our side.
      </p>
    </div>
    """.strip()

    try:
        resend.Emails.send({
            "from": EMAIL_FROM,
            "to": [req.email],
            "subject": f"Your {req.provider} statement breakdown — Tavon Partners",
            "html": html_body,
        })
    except Exception as exc:  # noqa: BLE001 - never log the email body/address on failure
        log.warning("email-report send failed: %s", type(exc).__name__)
        raise HTTPException(502, "Could not send that email right now - please try again shortly.")

    return {"sent": True}
