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
# Where "Quote me better price" lead notifications land - defaults to
# Tavon's own inbox, overridable via env var without a code change.
LEAD_NOTIFY_EMAIL = os.environ.get("LEAD_NOTIFY_EMAIL", "tavonpartners@gmail.com")

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
        return {
            "processor": "Clover / First Data",
            "turnover": turnover,
            "interchange_charges": interchange_items, "interchange_charges_stated": interchange_stated,
            "service_charges": charge_items, "service_charges_stated": charge_stated,
            "fees": fee_items, "fees_stated": fee_stated,
            "true_total_fees": round(true_total, 2),
            "true_blended_rate_pct": true_blended_rate,
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
