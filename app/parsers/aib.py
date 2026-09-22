"""
AIB Merchant Services parser.
Validated against real statement: Adamson Doors, Nov 2023 (via a live run
of pdftotext -layout against the actual PDF - not hand-typed test lines).

CRITICAL (per Kos, confirmed against real data): AIB's printed "MSC Rate (%)"
column is NOT the merchant's real rate - it's only the acquirer's own markup.
Interchange Fee and Scheme Fee are stacked on top of that in the same row and
are NOT reflected in the printed rate. The only correct effective rate per
row is:

    true_rate = abs(Total Charge) / Turnover

On the one real statement checked, the printed rate (0.28% on every row) was
1.8x to 4.6x LOWER than the true rate depending on card type. This parser
never surfaces the printed MSC Rate as "the rate" - it always computes the
true rate from Total Charge / Turnover.

Real row shape (confirmed against pdftotext -layout on the actual PDF):
description and every numeric column sit on ONE line starting with
"Mark up transaction", immediately followed by 4 lines of fixed
boilerplate ("currency GBP <Scheme> Sales <Category> Card Interchange
Card Scheme Fee") that carry the card scheme and category, wrapped
awkwardly across those lines. A prior version of this file assumed the
description and numbers were split across separate lines - that was
never actually checked against a real extraction and has been corrected
here.
"""
import re

# Substring match against the 4-line wrap joined together, since the exact
# line-break position shifts a little between Mastercard/Visa rows.
#
# KNOWN GAP (checked 2026-09, per Kos): this only covers the 2x2 matrix
# actually observed on the one real statement checked (Adamson Doors) -
# Commercial/Consumer x Debit/Credit. That statement had zero international
# transactions, so it's genuinely unknown whether AIB's MSC table has a
# distinguishable international row type at all, or folds it silently into
# one of these four categories with no visible marker. Needs a real AIB
# statement that actually contains international transactions before this
# can be fixed properly - do not guess a keyword here without one.
KEYWORD_CATEGORY = [
    ("Commercial Debit", "business_debit"),
    ("Commercial Credit", "business_credit"),
    ("Consumer Credit", "credit"),
    ("Consumer Debit", "debit"),
]

NUMERIC_ROW = re.compile(
    r"^Mark up transaction\s+"
    r"(?P<trx>\d+)\s+"
    r"(?P<turnover>[\d,]+\.\d{2})\s+"
    r"(?P<msc_rate>[\d.]+)\s+"
    r"(?P<fixed_rate>-?[\d.]+)\s+"
    r"(?P<charge_amt>-?[\d,]+\.\d{2})\s+"
    r"(?P<ichange>-?[\d,]+\.\d{2})\s+"
    r"(?P<scheme>-?[\d,]+\.\d{2})\s+"
    r"(?P<total>-?[\d,]+\.\d{2})\s+"
    r"GBP\s*$"
)
TOTAL_MSC_ROW = re.compile(
    r"^TOTAL MSC CHARGE\s+(?P<trx>\d+)\s+(?P<turnover>[\d,]+\.\d{2})\s+"
    r"(?P<charge_amt>-?[\d,]+\.\d{2})\s+(?P<ichange>-?[\d,]+\.\d{2})\s+"
    r"(?P<scheme>-?[\d,]+\.\d{2})\s+(?P<total>-?[\d,]+\.\d{2})\s+GBP\s*$"
)

# "Fees and Charges" table (separate from the MSC/card-fees table above) -
# this is where the Authorisation fee (a small per-transaction charge, e.g.
# GBP 0.01-0.03) and the Monthly Management Fee (a flat fee, e.g. GBP 9.50)
# live. Confirmed via a live pdftotext -layout run against two real
# statements (Adamson Doors and Moss Grove Dental Practice, both Nov 2023):
# each fee prints as one numeric line (description, count, fee amount,
# fee total, [VAT % and VAT amount columns are blank on every real example
# seen], total amount, currency) immediately followed by ONE wrap line
# holding the fee's date-range period, e.g. "31.10.2023 - 29.11.2023".
# These were previously not parsed at all, so they never reached
# "computed_total" or the frontend's "Other fees" figure.
FEE_CHARGE_ROW = re.compile(
    r"^(?P<desc>[A-Za-z][A-Za-z ]+?)\s+"
    r"(?P<count>\d+)\s+"
    r"(?P<fee_amount>[\d,]+\.\d{2})\s+"
    r"(?P<fee_total>-?[\d,]+\.\d{2})\s+"
    r"(?P<total_amount>-?[\d,]+\.\d{2})\s+"
    r"GBP\s*$"
)
TOTAL_FEES_ROW = re.compile(
    r"^TOTAL FEES CHARGED\s+(?P<count>\d+)\s+(?P<fee_total>-?[\d,]+\.\d{2})\s+"
    r"(?P<vat_amount>-?[\d,]+\.\d{2})\s+(?P<total_amount>-?[\d,]+\.\d{2})\s+GBP\s*$"
)

MAX_WRAP_LINES = 6  # real wrap is always 4 lines; a small safety margin


def _f(s):
    return float(s.replace(",", ""))


def _categorize(wrap_text):
    for keyword, category in KEYWORD_CATEGORY:
        if keyword in wrap_text:
            return category
    return "unmapped"


def parse_msc_table(lines):
    """lines: raw text lines of the statement, exactly as
    `pdftotext -layout` extracts them - including page headers/footers,
    which this function simply ignores outside of an open record.

    Each MSC record is: one "Mark up transaction ..." numeric line,
    followed immediately by up to 4 lines of boilerplate description
    wrap. Collection of that wrap is bounded (stops at a blank line, the
    next numeric row, the TOTAL row, or a safety cap) so that unrelated
    page furniture between statement pages never leaks into a record.
    """
    items = []
    stated_total = None
    pending_numeric = None
    pending_wrap = []

    def close_pending():
        if pending_numeric is None:
            return
        m = pending_numeric
        wrap_text = " ".join(pending_wrap)
        turnover = _f(m.group("turnover"))
        total_charge = _f(m.group("total"))
        printed_rate = float(m.group("msc_rate"))
        true_rate = abs(total_charge) / turnover * 100 if turnover else None
        scheme = "Mastercard" if "Mastercard" in wrap_text else (
            "Visa" if "Visa" in wrap_text else "unknown")
        items.append({
            "description": f"Mark up transaction ({scheme})" if scheme != "unknown" else "Mark up transaction",
            "detail": wrap_text,
            "category": _categorize(wrap_text),
            "trx": int(m.group("trx")),
            "turnover": turnover,
            "printed_msc_rate_pct": printed_rate,   # NEVER use this as "the rate"
            "true_effective_rate_pct": round(true_rate, 4) if true_rate else None,
            "charge_amount": _f(m.group("charge_amt")),
            "ichange_fee": _f(m.group("ichange")),
            "scheme_fee": _f(m.group("scheme")),
            "total_charge": total_charge,
        })

    for raw in lines:
        line = raw.strip()

        tm = TOTAL_MSC_ROW.match(line) if line else None
        if tm:
            close_pending()
            pending_numeric = None
            pending_wrap = []
            stated_total = {
                "trx": int(tm.group("trx")),
                "turnover": _f(tm.group("turnover")),
                "charge_amount": _f(tm.group("charge_amt")),
                "ichange_fee": _f(tm.group("ichange")),
                "scheme_fee": _f(tm.group("scheme")),
                "total_charge": _f(tm.group("total")),
            }
            continue

        m = NUMERIC_ROW.match(line) if line else None
        if m:
            close_pending()
            pending_numeric = m
            pending_wrap = []
            continue

        if pending_numeric is not None:
            if not line or len(pending_wrap) >= MAX_WRAP_LINES:
                close_pending()
                pending_numeric = None
                pending_wrap = []
            else:
                pending_wrap.append(line)
        # else: unrelated page furniture between records - ignore.

    close_pending()
    return items, stated_total


def parse_fees_and_charges(lines):
    """Parses the separate "Fees and Charges" table - Authorisation fee
    and Monthly Management Fee. Same wrap pattern as parse_msc_table but
    shorter (each fee's numeric line is followed by exactly one wrap line
    holding its date-range period), confirmed against two real statements.
    """
    items = []
    stated_total = None
    pending_numeric = None
    pending_wrap = []

    def close_pending():
        if pending_numeric is None:
            return
        m = pending_numeric
        items.append({
            "description": m.group("desc").strip(),
            "period": " ".join(pending_wrap).strip() or None,
            "count": int(m.group("count")),
            "fee_amount": _f(m.group("fee_amount")),
            "fee_total": _f(m.group("fee_total")),
            "total_amount": _f(m.group("total_amount")),
        })

    for raw in lines:
        line = raw.strip()

        tm = TOTAL_FEES_ROW.match(line) if line else None
        if tm:
            close_pending()
            pending_numeric = None
            pending_wrap = []
            stated_total = {
                "count": int(tm.group("count")),
                "fee_total": _f(tm.group("fee_total")),
                "vat_amount": _f(tm.group("vat_amount")),
                "total_amount": _f(tm.group("total_amount")),
            }
            continue

        m = FEE_CHARGE_ROW.match(line) if line else None
        if m:
            close_pending()
            pending_numeric = m
            pending_wrap = []
            continue

        if pending_numeric is not None:
            if not line or len(pending_wrap) >= MAX_WRAP_LINES:
                close_pending()
                pending_numeric = None
                pending_wrap = []
            else:
                pending_wrap.append(line)
        # else: unrelated page furniture between records - ignore.

    close_pending()
    return items, stated_total
