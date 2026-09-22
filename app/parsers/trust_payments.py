"""
Trust Payments (POS Statement) parser.
Validated against the real Bellis statement (Oct 2023).

Two real gotchas confirmed on this statement:

1. CHARACTER-INTERLEAVING BUG (previously flagged, confirmed here): the
   "Cross-Border Fees" row's first printed occurrence gets a page-footer
   string ("our Customer Support Dept:") interleaved into its own text
   ("Cross-Border" / "our Customer Support Dept:" / "Fees" all jumbled
   together) because of a page-break collision in the PDF layout. USEFUL
   FIX: every table on this statement (Visa Sales, Mastercard Sales,
   Ancillary Services & Fees) is printed TWICE - once as a possibly-cut-
   off preview before a page break, and then again in full immediately
   after ("Further breakdown provided in the following pages"). The
   second, later occurrence of each header is always the complete, clean
   one. Rather than trying to de-interleave characters, this parser
   simply always uses the LAST occurrence of each section header, which
   sidesteps the bug entirely on the one real statement checked.

2. Negative numbers use a Unicode soft hyphen (U+00AD), not a plain
   ASCII "-", e.g. "\xad1.02" for -1.02. Every numeric field is passed
   through a normalising float-parser that treats \xad the same as "-".
"""
import re

SOFT_HYPHEN = "\u00ad"


def _f(s):
    """Normalises a soft hyphen to a real minus sign, strips thousands
    commas, and returns 0.0 for the "//" not-applicable placeholder."""
    s = s.strip()
    if s == "//":
        return None
    s = s.replace(SOFT_HYPHEN, "-").replace(",", "")
    return float(s)


NUM = r"(?:{sh}|-)?[\d,]+\.\d{{2}}".format(sh=SOFT_HYPHEN)

CARD_SALES_ROW = re.compile(
    r"^(?P<region>Domestic|Inter|Intra)\s+(?P<desc>.+?)\s+"
    r"(?P<count>\d[\d,]*)\s+"
    r"(?P<gross>[\d,]+\.\d{2})\s+"
    r"(?P<trx_fees>" + NUM + r")\s+"
    r"(?P<ic>" + NUM + r")\s+"
    r"(?P<csf>" + NUM + r")\s+"
    r"(?P<total>" + NUM + r")\s*$"
)
CARD_SALES_TOTAL = re.compile(
    r"^Total\s+(?P<count>\d[\d,]*)\s+"
    r"(?P<gross>[\d,]+\.\d{2})\s+"
    r"(?P<trx_fees>" + NUM + r")\s+"
    r"(?P<ic>" + NUM + r")\s+"
    r"(?P<csf>" + NUM + r")\s+"
    r"(?P<total>" + NUM + r")\s*$"
)

# Ancillary/Adjustments/Misc share this 4-numeric-column shape: desc,
# count, gross (or "//"), fee (or "//"), total.
SIMPLE_FEE_ROW = re.compile(
    r"^(?P<desc>.+?)\s+(?P<count>\d[\d,]*)\s+"
    r"(?P<gross>//|[\d,]+\.\d{2})\s+"
    r"(?P<fee>//|" + NUM + r")\s+"
    r"(?P<total>" + NUM + r")\s*$"
)
SIMPLE_FEE_ROW_NUMERIC_ONLY = re.compile(
    r"^(?P<count>\d[\d,]*)\s+"
    r"(?P<gross>//|[\d,]+\.\d{2})\s+"
    r"(?P<fee>//|" + NUM + r")\s+"
    r"(?P<total>" + NUM + r")\s*$"
)
SIMPLE_FEE_TOTAL = re.compile(
    r"^Total\s+(?P<count>\d[\d,]*)\s+"
    r"(?P<gross>//|[\d,]+\.\d{2})\s+"
    r"(?P<fee>//|" + NUM + r")\s+"
    r"(?P<total>" + NUM + r")\s*$"
)

SUMMARY_LABELS = [
    "Total Settlement Amount", "Processing Fees", "Ancillary Services",
    "Ancillary Fees", "Net Activity", "Opening Balance",
    "Settlement Amount", "Closing Balance",
]


def _last_index(lines, header):
    idx = None
    for i, raw in enumerate(lines):
        if raw.strip() == header:
            idx = i
    return idx


def parse_summary(lines):
    """Processing Summary block near the top of the statement."""
    summary = {}
    for raw in lines:
        line = raw.strip()
        for label in SUMMARY_LABELS:
            if line.startswith(label):
                rest = line[len(label):].strip()
                m = re.search(r"(" + NUM + r")\s*$", rest)
                if m:
                    summary[label] = _f(m.group(1))
                break

    # "Sales" line has its own shape: "Sales   <count>   <amount>"
    for raw in lines:
        line = raw.strip()
        if line.startswith("Sales"):
            m = re.match(r"^Sales\s+(?P<count>\d[\d,]*)\s+(?P<amount>[\d,]+\.\d{2})\s*$", line)
            if m:
                summary["Sales_count"] = int(m.group("count").replace(",", ""))
                summary["Sales_amount"] = _f(m.group("amount"))
            break
    return summary


def parse_card_sales_table(lines, scheme_header):
    """Parses the Visa Sales / Mastercard Sales table. Always starts from
    the LAST occurrence of `scheme_header` in the document, to sidestep
    the interleaving bug (see module docstring) and avoid a possibly
    page-truncated first occurrence."""
    start = _last_index(lines, scheme_header)
    if start is None:
        return [], None

    items = []
    stated = None
    for raw in lines[start + 1:]:
        line = raw.strip()
        if not line:
            continue

        tm = CARD_SALES_TOTAL.match(line)
        if tm:
            stated = {
                "count": int(tm.group("count").replace(",", "")),
                "gross": _f(tm.group("gross")),
                "trx_fees": _f(tm.group("trx_fees")),
                "ic": _f(tm.group("ic")),
                "csf": _f(tm.group("csf")),
                "total": _f(tm.group("total")),
            }
            break

        m = CARD_SALES_ROW.match(line)
        if m:
            items.append({
                "region": m.group("region"),
                "description": m.group("desc").strip().replace(SOFT_HYPHEN, "-"),
                "count": int(m.group("count").replace(",", "")),
                "gross": _f(m.group("gross")),
                "trx_fees": _f(m.group("trx_fees")),
                "ic": _f(m.group("ic")),
                "csf": _f(m.group("csf")),
                "total": _f(m.group("total")),
            })
        # else: table column-header line or page furniture - ignore.

    return items, stated


def parse_simple_fee_table(lines, section_header):
    """Ancillary Services & Fees / Adjustments > Fees / Misc. - all share
    the same 4-numeric-column row shape. Uses the LAST occurrence of the
    header for the same interleaving-avoidance reason as the card tables.
    Handles the wrap case where a description splits across a comma-free
    prefix line, the numeric row, and a suffix line
    (e.g. "Annual Compliance" / numbers / "Fees")."""
    start = _last_index(lines, section_header)
    if start is None:
        return [], None

    items = []
    stated = None
    pending_prefix = None
    section_lines = lines[start + 1:]
    i = 0
    while i < len(section_lines):
        line = section_lines[i].strip()
        i += 1
        if not line:
            continue
        # Stop if we've run into the next section header entirely.
        if line in ("Adjustments", "Misc.", "Fees") and pending_prefix is None and not items:
            continue  # sub-header lines before the column header - skip

        tm = SIMPLE_FEE_TOTAL.match(line)
        if tm:
            stated = {
                "count": int(tm.group("count").replace(",", "")),
                "gross": _f(tm.group("gross")),
                "fee": _f(tm.group("fee")),
                "total": _f(tm.group("total")),
            }
            break

        m = SIMPLE_FEE_ROW.match(line)
        if m:
            pending_prefix = None
            items.append({
                "description": m.group("desc").strip().replace(SOFT_HYPHEN, "-"),
                "count": int(m.group("count").replace(",", "")),
                "gross": _f(m.group("gross")),
                "fee": _f(m.group("fee")),
                "total": _f(m.group("total")),
            })
            continue

        if pending_prefix is not None:
            m2 = SIMPLE_FEE_ROW_NUMERIC_ONLY.match(line)
            if m2:
                desc = pending_prefix
                if i < len(section_lines):
                    nxt = section_lines[i].strip()
                    if nxt and not re.search(r"\d", nxt) and nxt not in ("Total",):
                        desc = f"{pending_prefix} {nxt}"
                        i += 1
                items.append({
                    "description": desc.replace(SOFT_HYPHEN, "-"),
                    "count": int(m2.group("count").replace(",", "")),
                    "gross": _f(m2.group("gross")),
                    "fee": _f(m2.group("fee")),
                    "total": _f(m2.group("total")),
                })
                pending_prefix = None
                continue
            pending_prefix = None

        # Bare text line with no digits at all - buffer as a wrap prefix.
        if not re.search(r"\d", line) and line not in ("Type", "Fees", "Adjustments", "Misc."):
            pending_prefix = line

    return items, stated


def _categorize_row(region, desc):
    """Classifies a single card-sales row into the same taxonomy used
    across every other processor (debit, credit, business_debit,
    business_credit, international).

    The `region` field ("Domestic" / "Inter" / "Intra") is real, confirmed
    data straight from the statement's own row structure - "Inter" is
    Trust Payments' own term for international, and takes priority over
    any business/consumer signal, matching how Elavon/Clover/Dojo treat
    NON-EEA rows: Tavon's buy rates only have one international rate
    regardless of the underlying card's business/consumer status.

    The business/consumer split below follows the same keyword-matching
    approach already proven on Elavon, Dojo and Global Payments, but
    UNLIKE those, hasn't yet been confirmed against a real Trust Payments
    description string - no real "business"/"commercial"-labelled row was
    available to check this against when this was written. Treat the
    international detection as solid; treat business_credit/business_debit
    from this function as a reasonable best guess pending that check.
    """
    if region == "Inter":
        return "international"
    upper = desc.upper()
    is_business = any(k in upper for k in ("BUSINESS", "COMMERCIAL", "CORPORATE", "PURCHASING"))
    is_debit = "DEBIT" in upper
    is_credit = "CREDIT" in upper
    if is_business and is_debit:
        return "business_debit"
    if is_business and is_credit:
        return "business_credit"
    if is_debit:
        return "debit"
    if is_credit:
        return "credit"
    return "unmapped"


def parse_statement(raw_text):
    lines = raw_text.splitlines()
    summary = parse_summary(lines)

    visa_items, visa_total = parse_card_sales_table(lines, "Visa Sales")
    mc_items, mc_total = parse_card_sales_table(lines, "Mastercard Sales")
    ancillary_items, ancillary_total = parse_simple_fee_table(lines, "Ancillary Services & Fees")

    for item in visa_items + mc_items:
        item["category"] = _categorize_row(item["region"], item["description"])

    all_card_items = visa_items + mc_items
    total_gross = sum(i["gross"] for i in all_card_items) or None
    international_volume = sum(i["gross"] for i in all_card_items if i["category"] == "international")
    business_volume = sum(i["gross"] for i in all_card_items if i["category"] in ("business_debit", "business_credit"))

    true_processing_fees = round(
        (visa_total["trx_fees"] + visa_total["ic"] + visa_total["csf"] if visa_total else 0)
        + (mc_total["trx_fees"] + mc_total["ic"] + mc_total["csf"] if mc_total else 0), 2
    )

    return {
        "summary": summary,
        "visa_sales": visa_items, "visa_sales_stated": visa_total,
        "mastercard_sales": mc_items, "mastercard_sales_stated": mc_total,
        "ancillary": ancillary_items, "ancillary_stated": ancillary_total,
        "true_processing_fees_total": true_processing_fees,
        "turnover": total_gross,
        "international_volume": round(international_volume, 2),
        "business_volume": round(business_volume, 2),
    }
