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
import asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
import resend
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr, Field

from app.parsers import aib, clover, clover_fees, elavon, global_payments, intercard, dojo, trust_payments as trustpay, dna_payments, evo

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
# Where "Quote me better price" lead notifications land - defaults to
# Tavon's own inbox, overridable via env var without a code change.
LEAD_NOTIFY_EMAIL = os.environ.get("LEAD_NOTIFY_EMAIL", "tavonpartners@gmail.com")

# Google Contacts OAuth — the client credentials from the "Tavon Contacts"
# OAuth client in Google Cloud Console. Tokens themselves are never kept
# here (this whole service is built to persist nothing — see the module
# docstring); they're written straight to Supabase instead, using the
# service key below, which bypasses RLS the same way the Netlify
# functions' service key does.
GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
GOOGLE_OAUTH_REDIRECT_URI = os.environ.get(
    "GOOGLE_OAUTH_REDIRECT_URI", "https://tavon-statement-backend.onrender.com/oauth/google/callback"
)
PORTAL_URL = os.environ.get("PORTAL_URL", "https://tavonpartners.com/portal.html")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

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
    ("dna_payments", re.compile(r"DNA Payments Limited")),
    ("evo", re.compile(r"EVO Payments International|evopayments\.com")),
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
        # The MSC/card-fees table above is only PART of the real cost -
        # the separate "Fees and Charges" table (Authorisation fee,
        # Monthly Management Fee) sits alongside it and was previously
        # excluded from computed_total entirely. Fold it in for the true
        # total, same principle as Elavon/Trust Payments' hidden fee
        # sections.
        fee_items, fee_stated = aib.parse_fees_and_charges(lines)
        fee_charges_total = sum(i["total_amount"] for i in fee_items)
        true_total = computed + fee_charges_total
        turnover = sum(i["turnover"] for i in items) or None
        true_blended_rate = (
            round(abs(true_total) / turnover * 100, 4) if turnover else None
        )
        return {"processor": "AIB Merchant Services", "rows": items,
                "computed_total": round(computed, 2), "stated_total": stated,
                "fee_charges": fee_items, "fee_charges_stated": fee_stated,
                "true_total_fees": round(true_total, 2),
                "true_blended_rate_pct": true_blended_rate}

    if processor == "intercard":
        items, stated = intercard.parse_intercard_statement(lines)
        computed = sum(i["price"] for i in items) if items else 0
        return {"processor": "InterCard AG", "rows": items,
                "computed_total": round(computed, 2), "stated_total": stated}

    if processor == "clover":
        summary = clover.parse_summary(text)
        transaction_count = clover.parse_transaction_count(lines)
        interchange_items, interchange_stated = clover.parse_interchange_charges(lines)
        charge_items, charge_stated = clover.parse_service_charges(lines)
        fee_items, fee_stated = clover_fees.parse_fees(lines)
        # True total cost = Interchange Charges + Service Charges + Fees,
        # all three of which sit alongside each other on the statement's
        # own summary box - none is "the" fee total on its own. Prefer
        # each section's own stated total (closer to the source) over a
        # re-summed figure, falling back to the summary box or a re-sum
        # if a section's total line wasn't found for some reason.
        interchange_total = interchange_stated if interchange_stated is not None else (
            summary.get("interchange_stated", 0) or sum(i["fee"] for i in interchange_items))
        service_total = charge_stated if charge_stated is not None else (
            summary.get("service_charges_stated", 0) or sum(i["fee"] for i in charge_items))
        fees_total = fee_stated if fee_stated is not None else (
            summary.get("fees_stated", 0) or sum(i["fee"] for i in fee_items))
        true_total = interchange_total + service_total + fees_total
        turnover = summary.get("turnover")
        true_blended_rate = (
            round(abs(true_total) / turnover * 100, 4) if turnover else None
        )
        # Per Kos: these statements don't break out a domestic/international
        # split directly, but a "... INT ACCEPTANCE FEE" line in the Fees
        # section (Visa and/or Mastercard) carries the volume of card
        # transactions the scheme itself flagged as international - use
        # that as a proxy. Confirmed present with a real volume figure on
        # the Anne Urry statement (£3,475.80 of £25,194.90 turnover); absent
        # entirely on two other real statements checked, and present but
        # missing its volume figure on a real Chain-format statement (same
        # gap as the Service Charges table). Sums to 0 when unavailable,
        # which correctly falls back to "no international detected".
        international_volume = sum(
            f.get("volume") or 0 for f in fee_items
            if "INT" in f["description"].upper() and "ACCEPTANCE FEE" in f["description"].upper()
        )
        return {
            "processor": "Clover / First Data",
            "turnover": turnover,
            "transaction_count": transaction_count,
            "international_volume": round(international_volume, 2),
            "interchange_charges": interchange_items, "interchange_charges_stated": interchange_stated,
            "service_charges": charge_items, "service_charges_stated": charge_stated,
            "fees": fee_items, "fees_stated": fee_stated,
            "true_total_fees": round(true_total, 2),
            "true_blended_rate_pct": true_blended_rate,
        }

    if processor == "dna_payments":
        turnover, deductions_total = dna_payments.parse_summary(lines)
        items, a2_stated = dna_payments.parse_a2_by_type(lines)
        # Prefer the Deductions Summary total (already combines Transactional
        # + Recurring + Non-recurring + Other merchant fees) over the A2
        # table's own total, which only covers Transactional fees - on this
        # statement they're equal since B/C/D are all zero, but that won't
        # always be true.
        true_total = deductions_total if deductions_total is not None else (
            a2_stated["total"] if a2_stated else sum(i["total"] for i in items))
        true_blended_rate = (
            round(abs(true_total) / turnover * 100, 4) if turnover else None
        )
        transaction_count = a2_stated["count"] if a2_stated else None
        return {
            "processor": "DNA Payments",
            "turnover": turnover,
            "transaction_count": transaction_count,
            "rows": items,
            "true_total_fees": round(true_total, 2) if true_total is not None else None,
            "true_blended_rate_pct": true_blended_rate,
        }

    if processor == "evo":
        result = evo.parse_evo_statement(text)
        turnover = result["turnover"]
        true_total = result["true_total_fees"]
        # Never stated directly anywhere on this statement - computed
        # exactly as Kos described: true total fees over turnover.
        true_blended_rate = (
            round(true_total / turnover * 100, 4) if turnover and true_total is not None else None
        )
        return {
            "processor": "EVO Payments International",
            "turnover": turnover,
            "total_msc": result["total_msc"],
            "other_fees": result["other_fees"],
            "true_total_fees": true_total,
            "true_blended_rate_pct": true_blended_rate,
            "transaction_count": result["transaction_count"],
            "rows": result["rows"],
            # Amex is billed separately by Amex directly - no EVO fee
            # applies, so it's surfaced only for the mix breakdown, not
            # folded into the blended rate above.
            "amex_turnover": result["amex_turnover"],
            "amex_transaction_count": result["amex_transaction_count"],
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


class EmailFeeRow(BaseModel):
    label: str
    volume: float | None = None
    fee: float | None = None
    rate_pct: float | None = None


class StatementFigures(BaseModel):
    """Fields shared by both the client-facing report email and the
    internal lead-notification email - the two templates differ only in
    header/recipient framing, not in how the statement figures render."""
    provider: str = "Merchant"
    # Not yet populated by any parser - no processor currently extracts a
    # statement date/period from the PDF. Field exists so both templates
    # are ready for it; falls back to generic wording until that parsing
    # work is done per-processor.
    statement_period: str | None = None
    turnover: float | None = None
    total_fees: float | None = None
    blended_rate: float | None = None
    transaction_count: int | None = None
    fee_rows: list[EmailFeeRow] = Field(default_factory=list, max_length=200)


class EmailReportRequest(StatementFigures):
    email: EmailStr


class LeadNotifyRequest(StatementFigures):
    name: str
    business: str = ""
    email: EmailStr
    phone: str = ""


def _gbp(n: float | None) -> str:
    """Matches the frontend's fmtGBP: absolute value always, since a fee
    is never meaningfully negative from the merchant's point of view."""
    if n is None:
        return "—"
    return "£{:,.2f}".format(abs(n))


def _pct(n: float | None) -> str:
    if n is None:
        return "—"
    return "{:.2f}%".format(abs(n))


def _figures_section_html(f: "StatementFigures", intro_line: str) -> str:
    """The part shared by both templates: intro line, the two highlighted
    stat cards, the fee breakdown table, and the blended-rate block."""
    fee_rows_html = "".join(
        f"""<tr>
              <td style="padding:10px 8px;border-bottom:1px solid #EEE;color:#222;font-size:13.5px">{r.label}</td>
              <td style="padding:10px 8px;border-bottom:1px solid #EEE;color:#555;font-size:13.5px;text-align:right">{_gbp(r.volume) if r.volume is not None else '—'}</td>
              <td style="padding:10px 8px;border-bottom:1px solid #EEE;color:#222;font-size:13.5px;text-align:right;font-weight:600">{_gbp(r.fee)}</td>
              <td style="padding:10px 8px;border-bottom:1px solid #EEE;color:#1E6FD9;font-size:13.5px;text-align:right;font-weight:600">{_pct(r.rate_pct) if r.rate_pct is not None else '—'}</td>
            </tr>"""
        for r in f.fee_rows
    ) or """<tr><td colspan="4" style="padding:14px 8px;color:#888;font-size:13.5px">No fee breakdown available for this statement.</td></tr>"""

    return f"""
    <!-- Intro + highlighted top-line figures -->
    <tr>
      <td style="padding:22px 28px 6px">
        <div style="font-size:15px;font-weight:600;color:#222;margin-bottom:16px">{intro_line}</div>
        <table role="presentation" style="width:100%;border-spacing:0">
          <tr>
            <td style="width:50%;padding:16px;background:#F4F8FF;border-radius:10px;text-align:center">
              <div style="font-size:20px;font-weight:700;color:#111;font-family:'Courier New',monospace">{_gbp(f.turnover)}</div>
              <div style="font-size:11.5px;color:#6E7A8C;margin-top:4px">Total revenue this statement</div>
            </td>
            <td style="width:12px"></td>
            <td style="width:50%;padding:16px;background:#F4F8FF;border-radius:10px;text-align:center">
              <div style="font-size:20px;font-weight:700;color:#111;font-family:'Courier New',monospace">{_gbp(f.total_fees)}</div>
              <div style="font-size:11.5px;color:#6E7A8C;margin-top:4px">Total fees charged</div>
            </td>
          </tr>
        </table>
      </td>
    </tr>

    <!-- Fee breakdown table -->
    <tr>
      <td style="padding:22px 28px 4px">
        <div style="font-size:13px;font-weight:700;letter-spacing:.03em;color:#111;text-transform:uppercase;margin-bottom:8px">Fee breakdown</div>
        <table role="presentation" style="width:100%;border-collapse:collapse">
          <tr>
            <th style="text-align:left;padding:6px 8px;font-size:11px;color:#8B96A5;text-transform:uppercase;letter-spacing:.03em;border-bottom:1px solid #DDD">Card type</th>
            <th style="text-align:right;padding:6px 8px;font-size:11px;color:#8B96A5;text-transform:uppercase;letter-spacing:.03em;border-bottom:1px solid #DDD">Volume</th>
            <th style="text-align:right;padding:6px 8px;font-size:11px;color:#8B96A5;text-transform:uppercase;letter-spacing:.03em;border-bottom:1px solid #DDD">Fees</th>
            <th style="text-align:right;padding:6px 8px;font-size:11px;color:#8B96A5;text-transform:uppercase;letter-spacing:.03em;border-bottom:1px solid #DDD">Rate</th>
          </tr>
          {fee_rows_html}
        </table>
      </td>
    </tr>

    <!-- Blended rate - the final takeaway, at the very bottom -->
    <tr>
      <td style="padding:22px 28px 26px">
        <table role="presentation" style="width:100%;background:#0E1E33;border-radius:12px">
          <tr>
            <td style="padding:20px;text-align:center">
              <div style="font-size:11.5px;letter-spacing:.1em;text-transform:uppercase;color:#9CC3F0;margin-bottom:6px">Blended rate</div>
              <div style="font-size:30px;font-weight:700;color:#7CC0FF;font-family:'Courier New',monospace">{_pct(f.blended_rate)}</div>
              <div style="font-size:12px;color:#B9C5D6;margin-top:6px">{('Based on ' + str(f.transaction_count) + ' transactions processed') if f.transaction_count else ''}</div>
            </td>
          </tr>
        </table>
      </td>
    </tr>
    """


def build_report_email_html(req: "EmailReportRequest") -> str:
    """Client-facing template - sent when a merchant clicks 'Send results
    to my email'."""
    intro_line = (
        f"Statement breakdown for {req.statement_period}"
        if req.statement_period else "Your statement breakdown"
    )

    return f"""
<div style="background:#F4F6F9;padding:28px 12px;font-family:Arial,Helvetica,sans-serif">
  <table role="presentation" style="max-width:600px;width:100%;margin:0 auto;background:#FFFFFF;border-radius:14px;overflow:hidden;border:1px solid #E4E8EE">

    <!-- Header: processor name + Tavon Partners branding -->
    <tr>
      <td style="padding:26px 28px 20px;border-bottom:1px solid #EEE">
        <table role="presentation" style="width:100%">
          <tr>
            <td style="vertical-align:middle">
              <div style="font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:#8B96A5;margin-bottom:4px">Statement checker</div>
              <div style="font-size:19px;font-weight:700;color:#111">{req.provider} Statement</div>
            </td>
            <td style="vertical-align:middle;text-align:right;white-space:nowrap">
              <span style="font-size:14px;font-weight:700;color:#111;letter-spacing:.04em;vertical-align:middle">TAVON PARTNERS</span>
            </td>
          </tr>
        </table>
      </td>
    </tr>

    {_figures_section_html(req, intro_line)}

    <!-- Footer -->
    <tr>
      <td style="padding:0 28px 26px">
        <p style="margin:0;font-size:11.5px;line-height:1.6;color:#9AA6B8">
          Sent at your request from the Tavon Partners Merchant Statement Checker. This information was not stored on our side.
          Questions? WhatsApp us on <a href="https://wa.me/447584503279" style="color:#1E6FD9">07584 503279</a>.
        </p>
      </td>
    </tr>

  </table>
</div>
""".strip()


def build_lead_email_html(req: "LeadNotifyRequest") -> str:
    """Internal template - sent to Tavon when a merchant clicks 'Quote me
    better price'. Same figures layout as the client email, with a client
    details block up top instead of the client-facing framing/footer."""
    intro_line = (
        f"Statement breakdown for {req.statement_period}"
        if req.statement_period else "Statement breakdown"
    )

    return f"""
<div style="background:#F4F6F9;padding:28px 12px;font-family:Arial,Helvetica,sans-serif">
  <table role="presentation" style="max-width:600px;width:100%;margin:0 auto;background:#FFFFFF;border-radius:14px;overflow:hidden;border:1px solid #E4E8EE">

    <!-- Header: "New Quote Request" + Tavon Partners branding -->
    <tr>
      <td style="padding:26px 28px 20px;border-bottom:1px solid #EEE">
        <table role="presentation" style="width:100%">
          <tr>
            <td style="vertical-align:middle">
              <div style="font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:#8B96A5;margin-bottom:4px">Statement checker lead</div>
              <div style="font-size:19px;font-weight:700;color:#111">New Quote Request</div>
            </td>
            <td style="vertical-align:middle;text-align:right;white-space:nowrap">
              <span style="font-size:14px;font-weight:700;color:#111;letter-spacing:.04em;vertical-align:middle">TAVON PARTNERS</span>
            </td>
          </tr>
        </table>
      </td>
    </tr>

    <!-- Client details -->
    <tr>
      <td style="padding:22px 28px 4px">
        <div style="font-size:13px;font-weight:700;letter-spacing:.03em;color:#111;text-transform:uppercase;margin-bottom:8px">Client details</div>
        <table role="presentation" style="width:100%;border-collapse:collapse">
          <tr><td style="padding:5px 8px 5px 0;color:#8B96A5;font-size:13px;width:100px">Name</td><td style="padding:5px 8px;color:#111;font-size:13.5px;font-weight:600">{req.name}</td></tr>
          <tr><td style="padding:5px 8px 5px 0;color:#8B96A5;font-size:13px">Business</td><td style="padding:5px 8px;color:#111;font-size:13.5px">{req.business or '—'}</td></tr>
          <tr><td style="padding:5px 8px 5px 0;color:#8B96A5;font-size:13px">Email</td><td style="padding:5px 8px;color:#111;font-size:13.5px"><a href="mailto:{req.email}" style="color:#1E6FD9">{req.email}</a></td></tr>
          <tr><td style="padding:5px 8px 5px 0;color:#8B96A5;font-size:13px">Phone</td><td style="padding:5px 8px;color:#111;font-size:13.5px">{req.phone or '—'}</td></tr>
        </table>
      </td>
    </tr>

    {_figures_section_html(req, intro_line)}

    <!-- Footer -->
    <tr>
      <td style="padding:0 28px 26px">
        <p style="margin:0;font-size:11.5px;line-height:1.6;color:#9AA6B8">
          Generated via the Merchant Statement Checker on tavonpartners.com. Reply directly to this email to reach the client.
        </p>
      </td>
    </tr>

  </table>
</div>
""".strip()


@app.post("/email-report")
async def email_report(req: EmailReportRequest):
    if not resend.api_key:
        # Fails loudly rather than pretending to send - RESEND_API_KEY
        # must be set as an env var on Render for this to work at all.
        log.error("email-report called but RESEND_API_KEY is not configured.")
        raise HTTPException(500, "Email delivery is not configured yet - please contact Tavon Partners directly.")

    html_body = build_report_email_html(req)

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


@app.post("/notify-lead")
async def notify_lead(req: LeadNotifyRequest):
    if not resend.api_key:
        log.error("notify-lead called but RESEND_API_KEY is not configured.")
        raise HTTPException(500, "Lead notification is not configured yet.")

    html_body = build_lead_email_html(req)

    try:
        resend.Emails.send({
            "from": EMAIL_FROM,
            "to": [LEAD_NOTIFY_EMAIL],
            # Set so a reply from Tavon's inbox goes straight to the client,
            # not back to the noreply-style sending address.
            "reply_to": [req.email],
            "subject": f"New quote request — {req.name} ({req.provider})",
            "html": html_body,
        })
    except Exception as exc:  # noqa: BLE001
        log.warning("notify-lead send failed: %s", type(exc).__name__)
        raise HTTPException(502, "Could not send that notification right now.")

    return {"sent": True}


# ── Google Contacts OAuth ──────────────────────────────────────────
#
# This is the connection handshake only — it gets a Google account
# authorized and its tokens safely stored in Supabase. The actual sync
# logic (pushing/pulling contacts, polling, conflict resolution, the
# "Tavon Partners TP" label, dedup) is a separate, larger piece of work
# that builds on top of this once the connection itself is confirmed
# solid — deliberately not rushed into the same change as the OAuth
# plumbing, since a bug in a sync engine can quietly duplicate or lose
# real client data in a way a bug here cannot.
#
# Security note: this service has no login system of its own, so
# /oauth/google/start isn't gated by a session check here. It's safe
# regardless, for two independent reasons: (1) the link is only ever
# surfaced inside the admin-only Communication/Account area of the
# portal, and (2) while the Google Cloud OAuth consent screen stays in
# "Testing" status, Google itself refuses to let anyone complete this
# flow except the specific test user configured there
# (tavonpartners@gmail.com) — an attacker who found this URL would be
# stopped by Google's own consent screen before ever reaching the
# callback below.

def _supabase_headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }


@app.get("/oauth/google/start")
async def google_oauth_start():
    if not GOOGLE_OAUTH_CLIENT_ID:
        raise HTTPException(500, "Google OAuth is not configured yet - GOOGLE_OAUTH_CLIENT_ID is not set.")
    params = {
        "client_id": GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": "https://www.googleapis.com/auth/contacts email",
        "access_type": "offline",
        # Forces the consent screen every time, which guarantees Google
        # actually returns a refresh_token — it otherwise only does
        # that on an account's very first authorization ever.
        "prompt": "consent",
    }
    query = urlencode(params)
    return RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{query}")


@app.get("/oauth/google/callback")
async def google_oauth_callback(code: str | None = None, error: str | None = None):
    if error:
        return RedirectResponse(f"{PORTAL_URL}?gcontacts=error&reason={error}")
    if not code:
        return RedirectResponse(f"{PORTAL_URL}?gcontacts=error&reason=no_code")
    if not (GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET and SUPABASE_URL and SUPABASE_SERVICE_KEY):
        log.error("google_oauth_callback: server not fully configured")
        return RedirectResponse(f"{PORTAL_URL}?gcontacts=error&reason=not_configured")

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            token_res = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": GOOGLE_OAUTH_CLIENT_ID,
                    "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
                    "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
                    "grant_type": "authorization_code",
                },
            )
            token_res.raise_for_status()
            tokens = token_res.json()
        except Exception as exc:  # noqa: BLE001 - never log the code/tokens themselves
            log.warning("google_oauth_callback: token exchange failed: %s", type(exc).__name__)
            return RedirectResponse(f"{PORTAL_URL}?gcontacts=error&reason=token_exchange_failed")

        access_token = tokens.get("access_token")
        refresh_token = tokens.get("refresh_token")
        expires_in = tokens.get("expires_in", 3600)
        if not access_token or not refresh_token:
            # No refresh_token usually means prompt=consent didn't force a
            # fresh grant - shouldn't happen given how /start builds the
            # URL, but fail loudly rather than store a half-working token.
            log.error("google_oauth_callback: token response missing access_token or refresh_token")
            return RedirectResponse(f"{PORTAL_URL}?gcontacts=error&reason=incomplete_token_response")

        try:
            userinfo_res = await client.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            userinfo_res.raise_for_status()
            google_email = userinfo_res.json().get("email")
        except Exception as exc:  # noqa: BLE001
            log.warning("google_oauth_callback: userinfo lookup failed: %s", type(exc).__name__)
            return RedirectResponse(f"{PORTAL_URL}?gcontacts=error&reason=userinfo_failed")

        if not google_email:
            return RedirectResponse(f"{PORTAL_URL}?gcontacts=error&reason=no_email")

        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()

        try:
            upsert_res = await client.post(
                f"{SUPABASE_URL}/rest/v1/tavon_google_oauth_tokens",
                headers={**_supabase_headers(), "Prefer": "resolution=merge-duplicates"},
                json={
                    "google_email": google_email,
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "expires_at": expires_at,
                },
                params={"on_conflict": "google_email"},
            )
            upsert_res.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - never log token values
            log.error("google_oauth_callback: failed to store tokens in Supabase: %s", type(exc).__name__)
            return RedirectResponse(f"{PORTAL_URL}?gcontacts=error&reason=storage_failed")

    return RedirectResponse(f"{PORTAL_URL}?gcontacts=connected&email={google_email}")


@app.get("/oauth/google/status")
async def google_oauth_status():
    if not (SUPABASE_URL and SUPABASE_SERVICE_KEY):
        return {"connected": False}
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            res = await client.get(
                f"{SUPABASE_URL}/rest/v1/tavon_google_oauth_tokens",
                headers=_supabase_headers(),
                params={"select": "google_email,connected_at,updated_at", "limit": "1"},
            )
            res.raise_for_status()
            rows = res.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("google_oauth_status: lookup failed: %s", type(exc).__name__)
            return {"connected": False}
    if not rows:
        return {"connected": False}
    row = rows[0]
    return {"connected": True, "google_email": row.get("google_email"), "connected_at": row.get("connected_at")}


# ── Google Contacts — the actual bidirectional sync ─────────────────
#
# Design (per Kos):
#   - The app is the master record. App -> Google -> iPhone (native
#     Google-account contact sync on the phone).
#   - App -> Google: every create/edit of a Tavon contact pushes to
#     Google under the "Tavon Partners TP" contact group — the real
#     group Kos had already built, not a separate one this sync
#     invented.
#   - Google -> App: polled every ~15 minutes, scoped strictly to that
#     group's members — nothing else in the account is ever fetched or
#     touched, so a personal contact sitting elsewhere in the same
#     Google account is completely invisible to this sync. A genuinely
#     newer edit on Google's side updates the Tavon record; a tie or an
#     older Google edit leaves the app's version alone — the app wins.
#   - A person added straight on the phone, with no matching Tavon
#     contact yet, is created here flagged needs_review=true (the
#     "Unsorted" bucket) rather than silently merged into anything.
#   - Deleting in the app deletes on Google too. Deleting on Google is
#     only ever flagged (deleted_on_google=true) here, never
#     auto-deletes the Tavon row — that decision stays a human one.
#   - Dedup is by Google's own resourceName, stored on the Tavon row.

PEOPLE_API = "https://people.googleapis.com/v1"
# The real, existing Google Contacts label — Kos built this himself
# with 900+ real business contacts already in it, long before this
# sync existed. Pushing new contacts here (rather than creating a
# separate "Tavon Clients" group) and scoping the pull to only this
# group's members is what keeps personal contacts elsewhere in the
# same account completely untouched by the sync.
TAVON_GROUP_NAME = "Tavon Partners TP"
PERSON_FIELDS = "names,phoneNumbers,emailAddresses,organizations,metadata"


async def _get_token_row(client: "httpx.AsyncClient") -> dict | None:
    res = await client.get(
        f"{SUPABASE_URL}/rest/v1/tavon_google_oauth_tokens",
        headers=_supabase_headers(),
        params={"select": "*", "limit": "1"},
    )
    res.raise_for_status()
    rows = res.json()
    return rows[0] if rows else None


async def get_valid_access_token(client: "httpx.AsyncClient") -> tuple[str, dict] | tuple[None, None]:
    """Returns a usable access token for the connected Google account,
    refreshing it first if it's expired or close to it. Returns
    (access_token, token_row) or (None, None) if nothing is connected."""
    row = await _get_token_row(client)
    if not row:
        return None, None
    expires_at = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
    if expires_at > datetime.now(timezone.utc) + timedelta(minutes=2):
        return row["access_token"], row
    try:
        refresh_res = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
                "refresh_token": row["refresh_token"],
                "grant_type": "refresh_token",
            },
        )
        refresh_res.raise_for_status()
        tokens = refresh_res.json()
    except Exception as exc:  # noqa: BLE001 - never log token values
        log.error("get_valid_access_token: refresh failed: %s", type(exc).__name__)
        return None, None
    new_access_token = tokens.get("access_token")
    new_expires_at = (datetime.now(timezone.utc) + timedelta(seconds=tokens.get("expires_in", 3600))).isoformat()
    row["access_token"] = new_access_token
    row["expires_at"] = new_expires_at
    try:
        await client.patch(
            f"{SUPABASE_URL}/rest/v1/tavon_google_oauth_tokens",
            headers=_supabase_headers(),
            params={"id": f"eq.{row['id']}"},
            json={"access_token": new_access_token, "expires_at": new_expires_at, "updated_at": datetime.now(timezone.utc).isoformat()},
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("get_valid_access_token: could not persist refreshed token: %s", type(exc).__name__)
    return new_access_token, row


async def ensure_tavon_clients_group(client: "httpx.AsyncClient", access_token: str, token_row: dict) -> str | None:
    """Returns the resourceName of the existing "Tavon Partners TP" group,
    only creating one (with that same name) in the rare case it's ever
    missing. Cached on the token row after the first lookup."""
    if token_row.get("contact_group_resource_name"):
        return token_row["contact_group_resource_name"]
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        list_res = await client.get(f"{PEOPLE_API}/contactGroups", headers=headers, params={"pageSize": 200})
        list_res.raise_for_status()
        for group in list_res.json().get("contactGroups", []):
            if group.get("name") == TAVON_GROUP_NAME:
                resource_name = group["resourceName"]
                break
        else:
            create_res = await client.post(
                f"{PEOPLE_API}/contactGroups", headers=headers, json={"contactGroup": {"name": TAVON_GROUP_NAME}}
            )
            create_res.raise_for_status()
            resource_name = create_res.json()["resourceName"]
    except Exception as exc:  # noqa: BLE001
        log.error("ensure_tavon_clients_group: failed: %s", type(exc).__name__)
        return None
    try:
        await client.patch(
            f"{SUPABASE_URL}/rest/v1/tavon_google_oauth_tokens",
            headers=_supabase_headers(),
            params={"id": f"eq.{token_row['id']}"},
            json={"contact_group_resource_name": resource_name},
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("ensure_tavon_clients_group: could not cache resourceName: %s", type(exc).__name__)
    return resource_name


def _person_body_from_contact(contact: dict) -> dict:
    body: dict = {}
    if contact.get("first_name") or contact.get("last_name"):
        body["names"] = [{"givenName": contact.get("first_name") or "", "familyName": contact.get("last_name") or ""}]
    if contact.get("phone"):
        body["phoneNumbers"] = [{"value": contact["phone"], "type": "mobile"}]
    if contact.get("email"):
        body["emailAddresses"] = [{"value": contact["email"]}]
    if contact.get("company"):
        body["organizations"] = [{"name": contact["company"]}]
    return body


class ContactPushRequest(BaseModel):
    id: str
    first_name: str | None = None
    last_name: str | None = None
    company: str | None = None
    phone: str | None = None
    email: str | None = None
    google_resource_name: str | None = None


@app.post("/contacts/push")
async def push_contact(req: ContactPushRequest):
    """Called by the portal right after a Tavon contact is created or
    updated. Best-effort by design — a Google outage shouldn't block
    saving a contact in the app, so failures here are reported but
    never meant to be treated as fatal by the caller."""
    async with httpx.AsyncClient(timeout=15) as client:
        access_token, token_row = await get_valid_access_token(client)
        if not access_token:
            raise HTTPException(409, "Google Contacts isn't connected yet.")
        group_resource_name = await ensure_tavon_clients_group(client, access_token, token_row)
        headers = {"Authorization": f"Bearer {access_token}"}
        body = _person_body_from_contact(req.model_dump())

        try:
            if req.google_resource_name:
                get_res = await client.get(
                    f"{PEOPLE_API}/{req.google_resource_name}", headers=headers, params={"personFields": "metadata"}
                )
                get_res.raise_for_status()
                body["etag"] = get_res.json()["etag"]
                update_fields = ",".join(k for k in ("names", "phoneNumbers", "emailAddresses", "organizations") if k in body)
                res = await client.patch(
                    f"{PEOPLE_API}/{req.google_resource_name}:updateContact",
                    headers=headers,
                    params={"updatePersonFields": update_fields or "names"},
                    json=body,
                )
                res.raise_for_status()
                resource_name = req.google_resource_name
            else:
                if group_resource_name:
                    body["memberships"] = [{"contactGroupMembership": {"contactGroupResourceName": group_resource_name}}]
                res = await client.post(f"{PEOPLE_API}/people:createContact", headers=headers, json=body)
                res.raise_for_status()
                resource_name = res.json()["resourceName"]
        except Exception as exc:  # noqa: BLE001
            log.warning("push_contact: Google API call failed: %s", type(exc).__name__)
            raise HTTPException(502, "Could not sync this contact to Google right now.")

        try:
            await client.patch(
                f"{SUPABASE_URL}/rest/v1/tavon_contacts",
                headers=_supabase_headers(),
                params={"id": f"eq.{req.id}"},
                json={"google_resource_name": resource_name, "google_updated_at": datetime.now(timezone.utc).isoformat()},
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("push_contact: could not record resourceName in Supabase: %s", type(exc).__name__)

    return {"synced": True, "google_resource_name": resource_name}


@app.post("/contacts/delete")
async def delete_contact(google_resource_name: str):
    """Called by the portal when a Tavon contact is deleted, so the
    deletion follows through to Google too."""
    if not google_resource_name:
        return {"deleted": False, "reason": "no_resource_name"}
    async with httpx.AsyncClient(timeout=15) as client:
        access_token, _ = await get_valid_access_token(client)
        if not access_token:
            raise HTTPException(409, "Google Contacts isn't connected yet.")
        try:
            res = await client.delete(
                f"{PEOPLE_API}/{google_resource_name}:deleteContact",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if res.status_code not in (200, 404):
                res.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            log.warning("delete_contact: Google API call failed: %s", type(exc).__name__)
            raise HTTPException(502, "Could not delete this contact on Google right now.")
    return {"deleted": True}


def _contact_from_person(person: dict) -> dict:
    names = person.get("names") or [{}]
    phones = person.get("phoneNumbers") or [{}]
    emails = person.get("emailAddresses") or [{}]
    orgs = person.get("organizations") or [{}]
    return {
        "first_name": names[0].get("givenName", ""),
        "last_name": names[0].get("familyName", ""),
        "phone": phones[0].get("value"),
        "email": emails[0].get("value"),
        "company": orgs[0].get("name"),
    }


async def run_contacts_pull():
    """The Google -> App half of the sync. Scoped strictly to members of
    the "Tavon Partners TP" group — anything else in the account (a
    personal contact, say) is never even fetched, let alone touched.
    Safe to call repeatedly; a no-op if nothing's connected."""
    async with httpx.AsyncClient(timeout=30) as client:
        access_token, token_row = await get_valid_access_token(client)
        if not access_token:
            return
        headers = {"Authorization": f"Bearer {access_token}"}
        group_resource_name = await ensure_tavon_clients_group(client, access_token, token_row)
        if not group_resource_name:
            return

        try:
            group_res = await client.get(
                f"{PEOPLE_API}/{group_resource_name}", headers=headers, params={"maxMembers": 10000}
            )
            group_res.raise_for_status()
            member_resource_names = group_res.json().get("memberResourceNames", [])
        except Exception as exc:  # noqa: BLE001
            log.warning("run_contacts_pull: could not list group members: %s", type(exc).__name__)
            return

        # Batch-fetch full details for exactly those members, 200 at a
        # time (the API's own limit per batchGet call) — never the
        # whole account.
        people: list[dict] = []
        for i in range(0, len(member_resource_names), 200):
            chunk = member_resource_names[i:i + 200]
            try:
                batch_res = await client.get(
                    f"{PEOPLE_API}/people:batchGet",
                    headers=headers,
                    params=[("resourceNames", r) for r in chunk] + [("personFields", PERSON_FIELDS)],
                )
                batch_res.raise_for_status()
                for item in batch_res.json().get("responses", []):
                    person = item.get("person")
                    if person:
                        people.append(person)
            except Exception as exc:  # noqa: BLE001
                log.warning("run_contacts_pull: batchGet failed for one chunk: %s", type(exc).__name__)
                continue

        fetched_resource_names = {p.get("resourceName") for p in people if p.get("resourceName")}

        for person in people:
            resource_name = person.get("resourceName")
            if not resource_name:
                continue
            try:
                match_res = await client.get(
                    f"{SUPABASE_URL}/rest/v1/tavon_contacts",
                    headers=_supabase_headers(),
                    params={"google_resource_name": f"eq.{resource_name}", "select": "id,updated_at", "limit": "1"},
                )
                match_res.raise_for_status()
                matches = match_res.json()
            except Exception as exc:  # noqa: BLE001
                log.warning("run_contacts_pull: lookup failed for one contact: %s", type(exc).__name__)
                continue

            if matches:
                contact_id = matches[0]["id"]
                # Conflict rule: only apply Google's version if it's
                # genuinely newer than the app's own last update — a tie
                # or an older Google edit leaves the app's version alone.
                sources = person.get("metadata", {}).get("sources", [{}])
                google_updated = sources[0].get("updateTime")
                app_updated = matches[0].get("updated_at")
                if google_updated and app_updated and google_updated <= app_updated:
                    continue
                patch = _contact_from_person(person)
                patch["google_updated_at"] = google_updated
                await client.patch(
                    f"{SUPABASE_URL}/rest/v1/tavon_contacts",
                    headers=_supabase_headers(),
                    params={"id": f"eq.{contact_id}"},
                    json=patch,
                )
            else:
                # No Tavon match at all - a contact already in this group
                # (or added straight on the phone, into this same group)
                # with no linked Tavon record yet. Lands as Unsorted for
                # a human to review, rather than being assumed to belong
                # to any particular lead or deal.
                new_contact = _contact_from_person(person)
                if not new_contact.get("first_name") and not new_contact.get("phone") and not new_contact.get("email"):
                    continue
                new_contact.update({
                    "google_resource_name": resource_name,
                    "origin": "google",
                    "needs_review": True,
                    "status": "lead",
                })
                await client.post(
                    f"{SUPABASE_URL}/rest/v1/tavon_contacts", headers=_supabase_headers(), json=new_contact
                )

        # Anything Tavon still thinks is linked to Google, but that no
        # longer shows up in this run's group member list, has either
        # been deleted on Google's side or removed from the group —
        # either way, flag it rather than silently keep treating it as
        # connected, and never auto-delete the Tavon record itself.
        try:
            linked_res = await client.get(
                f"{SUPABASE_URL}/rest/v1/tavon_contacts",
                headers=_supabase_headers(),
                params={"google_resource_name": "not.is.null", "deleted_on_google": "eq.false", "select": "id,google_resource_name"},
            )
            linked_res.raise_for_status()
            for row in linked_res.json():
                if row["google_resource_name"] not in fetched_resource_names:
                    await client.patch(
                        f"{SUPABASE_URL}/rest/v1/tavon_contacts",
                        headers=_supabase_headers(),
                        params={"id": f"eq.{row['id']}"},
                        json={"deleted_on_google": True},
                    )
        except Exception as exc:  # noqa: BLE001
            log.warning("run_contacts_pull: removal-detection pass failed: %s", type(exc).__name__)


async def _contacts_polling_loop():
    # Runs for as long as this process is alive. On Render's paid
    # "always on" tiers that's continuous; on a tier that spins down
    # between requests, this loop pauses along with the rest of the
    # process and simply resumes once a request wakes it again — each
    # run re-checks the Tavon group fresh (it's scoped small enough
    # that this is cheap), so nothing needs to be resumed from a saved
    # position, just delayed until the service is next awake.
    while True:
        try:
            await run_contacts_pull()
        except Exception as exc:  # noqa: BLE001
            log.error("contacts polling loop: unexpected error: %s", type(exc).__name__)
        await asyncio.sleep(900)  # 15 minutes


@app.on_event("startup")
async def _start_contacts_polling():
    if SUPABASE_URL and SUPABASE_SERVICE_KEY:
        asyncio.create_task(_contacts_polling_loop())


@app.post("/contacts/pull-now")
async def pull_now():
    """Manual trigger for testing the Google -> App pull without
    waiting for the 15-minute loop - not linked from the portal UI,
    call directly while verifying the sync works."""
    await run_contacts_pull()
    return {"ran": True}
