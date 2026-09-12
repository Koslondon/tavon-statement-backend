"""
Global Payments (GPUK LLP) statement parser.

Handles the two documented extraction hazards for this processor:
  1. The "TRANSACTIONS CHARGES" table header extracts out of visual order
     (fixed, known scramble for this table shape - hardcoded skip-past,
     not trusted as a live column-order signal).
  2. Some descriptions are SPLIT AROUND the numeric row itself, e.g.:
        VISA 3DS
        359 24,498.75 68.24 0.0000 0.0300 10.77 GBP
        AUTHENTICATION FEE
     This is genuinely ambiguous from text alone - a lone line between two
     numeric rows could complete the row before it OR start the row after
     it. Resolved here via a small hardcoded list of known, stable,
     card-scheme fee-name completions - NOT a general solution. A statement
     with an unfamiliar split phrase would need bounding-box/coordinate-
     aware extraction (pdfplumber extract_words() with position data) to
     resolve correctly, since that shows which line each word visually
     belongs to rather than guessing from text order.

CRITICAL (per Kos, confirmed against a real statement): the invoice has
THREE separate charge boxes, and the first one alone looks deceptively
cheap:

  1. TRANSACTIONS CHARGES ("normal rate") - printed rates of 0.35%-0.61%.
     This is `parse_transactions_charges()` below - the only box this
     parser originally covered.
  2. INTERCHANGE OTHER CHARGES - often has an EXTRA percentage stacked on
     top, embedded directly in the row's own description text (e.g.
     "VISA ECOM INT CR 1.0587%&0.0952"), and MOST rows here print no rate
     at all - the true cost is only visible as fee/amount.
  3. OTHER FEES - fixed per-transaction authorisation charges (VAT and
     non-VAT applicable sub-tables).

On the real statement checked (Ink N Toner UK, Oct 2022), these three
boxes total 411.28 + 675.69 + 137.91 = 1,224.88 - exactly matching the
invoice's own "CHARGE APPLIED TO ACCOUNT" line - against a "COMBINED
TRANSACTION CHARGE" of only 411.28. On sales of 66,004.57, that's a true
blended rate of 1.86%, roughly 3x what Box 1's printed rates alone would
suggest. `parse_full_statement()` combines all three boxes and always
reports the true total/blended rate, never just Box 1 in isolation.
"""
import re
from app.parsers.global_payments_categories import categorize

ROW_PATTERN = re.compile(
    r"^(?P<desc>.*?)\s*"
    r"(?P<items>\d+)\s+"
    r"(?P<amount>[\d,]+\.\d{2})\s+"
    r"(?P<atv>[\d,]+\.\d{2})\s+"
    r"(?P<pct_rate>[\d.]+)\s+"
    r"(?P<per_item_rate>[\d.]+)\s+"
    r"(?P<fee_amount>[\d,]+\.\d{2})\s+"
    r"(?P<currency>GBP)\s*$"
)
TOTAL_LINE = re.compile(r"^TOTAL\s+([\d,]+\.\d{2})\s+GBP\s*$")

# Box 2: Interchange. One shape covers every row regardless of whether a
# rate is embedded in the free-text description, since desc is captured
# generically - "VI UK COMMERCIAL DEBIT" and "VISA ECOM INT CR
# 1.0587%&0.0952" both just land in the desc group either way.
INTERCHANGE_ROW = re.compile(
    r"^(?P<desc>.+?)\s+(?P<items>\d[\d,]*)\s+"
    r"(?P<amount>[\d,]+\.\d{2})\s+(?P<fee>[\d,]+\.\d{2})\s+GBP\s*$"
)
INTERCHANGE_TOTAL = re.compile(r"^TOTAL\s+(?P<total>[\d,]+\.\d{2})\s+GBP\s*$")

# Box 3: Other Fees. CARD is optional (some rows have none); RATE is a
# fixed-format field that isn't always a real per-unit rate (occasionally
# it's a raw count, e.g. "MC ATH VOL"), so it's kept only for display.
OTHER_FEE_ROW = re.compile(
    r"^(?:(?P<card>[A-Z]+)\s+)?(?P<code>\d{4})\s+(?P<desc>.+?)\s+"
    r"(?P<number>[\d.]+)\s+(?P<rate>[\d.]+)\s+"
    r"(?P<amount>[\d,]+\.\d{2})\s+GBP\s*$"
)
OTHER_FEES_TOTAL = re.compile(r"^TOTAL\s+(?P<total>[\d,]+\.\d{2})\s+GBP\s*$")

SUMMARY_LABELS = [
    "COMBINED TRANSACTION CHARGE", "INTERCHANGE", "OTHER FEES",
    "MINIMUM ADJUSTMENT FEE", "CHARGE APPLIED TO ACCOUNT",
]

# Known stable card-scheme fee-name completions - used to resolve the
# "does this trailing line finish the row before it?" ambiguity safely.
KNOWN_SUFFIX_COMPLETIONS = {
    "AUTHENTICATION FEE",  # completes "VISA 3DS" / "MCARD 3DS2"
}


def _to_float(s: str) -> float:
    return float(s.replace(",", ""))


def parse_transactions_charges(raw_text: str):
    """Section-anchored to survive a page break in the middle of the
    table. Confirmed on a real statement (DavidCaine) where the
    TRANSACTIONS CHARGES header repeats after a page break, with real
    data rows on BOTH pages and only ONE closing TOTAL line covering all
    of them combined. An earlier version located the header only once,
    by finding the LAST "RATE" line in the whole document before
    scanning - which meant it jumped straight past the entire first
    page's rows whenever the header repeated, silently dropping real
    transactions (manually summing both pages here confirms they add up
    exactly to the statement's own TOTAL, so the fix is to keep
    resuming after every header repeat, not just the first)."""
    lines = [l.strip() for l in raw_text.splitlines() if l.strip()]

    line_items = []
    stated_total = None
    carry_prefix = ""
    scanning = False  # becomes True once we're past the first header block
    i = 0
    while i < len(lines):
        line = lines[i]

        if line == "TRANSACTIONS CHARGES":
            # Skip this header occurrence (first or a page-break repeat)
            # up to and including its own "RATE" anchor line, then resume
            # normal row scanning right after it.
            j = i + 1
            while j < len(lines) and lines[j] != "RATE":
                j += 1
            i = j + 1
            scanning = True
            carry_prefix = ""
            continue

        if not scanning:
            i += 1
            continue

        total_match = TOTAL_LINE.match(line)
        if total_match:
            stated_total = _to_float(total_match.group(1))
            break

        m = ROW_PATTERN.match(line)
        if m:
            desc = (carry_prefix + " " + m.group("desc")).strip()
            carry_prefix = ""

            # Peek ahead: does the next line complete this row's description?
            if i + 1 < len(lines) and lines[i + 1] in KNOWN_SUFFIX_COMPLETIONS:
                desc = (desc + " " + lines[i + 1]).strip()
                i += 1  # consume the suffix line

            cat = categorize(desc)
            line_items.append({
                "description": desc,
                "items": int(m.group("items")),
                "amount": _to_float(m.group("amount")),
                "atv": _to_float(m.group("atv")),
                "percent_rate": float(m.group("pct_rate")),
                "per_item_rate": float(m.group("per_item_rate")),
                "fee_amount": _to_float(m.group("fee_amount")),
                **cat,
            })
        else:
            # Pure text line, no numbers - buffer as prefix for the next
            # numeric row (unless already claimed as a known suffix above).
            carry_prefix = (carry_prefix + " " + line).strip()

        i += 1

    return line_items, stated_total


def parse_summary(raw_text):
    """Top-level 'SUMMARY OF CHARGES' box - the four components that add
    up to the true total, plus the sales volume needed for a blended rate."""
    lines = raw_text.splitlines()
    summary = {}
    for raw in lines:
        line = raw.strip()
        for label in SUMMARY_LABELS:
            if line.startswith(label):
                rest = line[len(label):].strip()
                m = re.search(r"([\d,]+\.\d{2})\s+DR\s*$", rest)
                if m:
                    summary[label] = _to_float(m.group(1))
                break

    m = re.search(r"SALES:\s+\d+\s+([\d,]+\.\d{2})", raw_text)
    if m:
        summary["sales_volume"] = _to_float(m.group(1))

    # "TRANSACTION ITEM SUMMARY" prints SALES/REFUNDS/TOTAL side by side with
    # DB ADJ/CR ADJ/TOTAL (pdftotext -layout merges each visual row into one
    # line), e.g. "TOTAL:   879   64,503.55   TOTAL:   0   0.00" - the double
    # "TOTAL:" on one line only ever occurs in this merged row, so it's a
    # safe anchor. Per Kos: this combined SALES+REFUNDS item count (879) is
    # the transaction count to report, not the Sales-only count (855) and
    # not the row count of the Box 1 fee table (which is one row per fee
    # category/scheme, not one row per transaction).
    m = re.search(r"TOTAL:\s+([\d,]+)\s+[\d,]+\.\d{2}\s+TOTAL:", raw_text)
    if m:
        summary["transaction_count"] = int(m.group(1).replace(",", ""))

    return summary


def parse_interchange_charges(raw_text):
    """Box 2: INTERCHANGE OTHER CHARGES. True rate is always fee/amount -
    a rate embedded in the description text (when present at all) is an
    ADDITIONAL cost on top of Box 1, not a substitute for computing the
    real per-row rate."""
    lines = raw_text.splitlines()
    items = []
    stated_total = None
    in_section = False
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line == "INTERCHANGE OTHER CHARGES":
            in_section = True
            continue
        if not in_section:
            continue

        tm = INTERCHANGE_TOTAL.match(line)
        if tm:
            stated_total = _to_float(tm.group("total"))
            break

        m = INTERCHANGE_ROW.match(line)
        if m:
            amount = _to_float(m.group("amount"))
            fee = _to_float(m.group("fee"))
            items.append({
                "description": m.group("desc").strip(),
                "items": int(m.group("items").replace(",", "")),
                "amount": amount,
                "fee": fee,
                "true_rate_pct": round(fee / amount * 100, 4) if amount else None,
            })
        # else: column-header / page-furniture line - ignore.

    return items, stated_total


def parse_other_fees(raw_text):
    """Box 3: OTHER FEES (both the VAT-applicable and non-VAT-applicable
    sub-tables share this row shape). Uses the LAST 'TOTAL' line in the
    section, since the VAT-applicable sub-table prints its own zero
    TOTAL/SUB TOTAL first when there's nothing chargeable there."""
    lines = raw_text.splitlines()
    items = []
    stated_total = None
    in_section = False
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line == "OTHER FEES":
            in_section = True
            continue
        if not in_section:
            continue

        tm = OTHER_FEES_TOTAL.match(line)
        if tm:
            stated_total = _to_float(tm.group("total"))
            continue  # keep going - the real total is the LAST one seen

        m = OTHER_FEE_ROW.match(line)
        if m:
            items.append({
                "card": m.group("card"),
                "code": m.group("code"),
                "description": m.group("desc").strip(),
                "number": m.group("number"),
                "rate_display": m.group("rate"),
                "amount": _to_float(m.group("amount")),
            })
        # else: column-header / sub-table label line - ignore.

    return items, stated_total


def parse_full_statement(raw_text):
    """Combines all three boxes into the true total cost and true blended
    rate - the number that actually matters, since Box 1 alone
    understates the real cost by roughly 3x on the real statement checked."""
    box1_items, box1_stated = parse_transactions_charges(raw_text)
    box2_items, box2_stated = parse_interchange_charges(raw_text)
    box3_items, box3_stated = parse_other_fees(raw_text)
    summary = parse_summary(raw_text)

    true_total = round(
        (box1_stated or 0) + (box2_stated or 0) + (box3_stated or 0)
        + summary.get("MINIMUM ADJUSTMENT FEE", 0), 2
    )
    sales_volume = summary.get("sales_volume")
    true_blended_rate = (
        round(true_total / sales_volume * 100, 4) if sales_volume else None
    )

    return {
        "summary": summary,
        "box1_transactions_charges": {"items": box1_items, "stated_total": box1_stated},
        "box2_interchange": {"items": box2_items, "stated_total": box2_stated},
        "box3_other_fees": {"items": box3_items, "stated_total": box3_stated},
        "true_total_cost": true_total,
        "stated_charge_applied_to_account": summary.get("CHARGE APPLIED TO ACCOUNT"),
        "sales_volume": sales_volume,
        "transaction_count": summary.get("transaction_count"),
        "true_blended_rate_pct": true_blended_rate,
        "box1_only_rate_pct": (
            round((box1_stated or 0) / sales_volume * 100, 4) if sales_volume else None
        ),
    }


if __name__ == "__main__":
    # Real raw pdfplumber extraction sample from the Ink N Toner statement,
    # exactly as supplied - used here as a regression test.
    sample = """Page 4 of 7
TRANSACTIONS CHARGES
GBP
PER
PERCENT FEE
DESCRIPTION ITEMS AMOUNT ATV ITEM CURRENCY
RATE AMOUNT
RATE
MCPP 3 297.85 99.28 0.6100 0.0500 1.97 GBP
MDCD 17 1,661.32 97.72 0.3500 0.0500 6.66 GBP
MDCD Merchandise Rtn 1 181.74 181.74 0.3500 0.0000 0.64 GBP
MCCP 52 6,979.19 134.22 0.6100 0.0500 45.18 GBP
MCCP Merchandise Rtn 2 144.46 72.23 0.6100 0.0000 0.88 GBP
MDPD 69 2,313.94 33.54 0.3500 0.0500 11.55 GBP
MCFL 72 8,236.71 114.40 0.6100 0.0500 53.84 GBP
MCFL Merchandise Rtn 2 289.24 144.62 0.6100 0.0000 1.76 GBP
MCGD 5 176.21 35.24 0.6100 0.0500 1.32 GBP
MCPC 50 5,321.71 106.43 0.3500 0.0500 21.13 GBP
MCPC Merchandise Rtn 1 4.99 4.99 0.3500 0.0000 0.02 GBP
MCNW 40 1,628.20 40.71 0.6100 0.0500 11.93 GBP
MCPL 32 1,560.34 48.76 0.6100 0.0500 11.12 GBP
MCPL Merchandise Rtn 1 17.99 17.99 0.6100 0.0000 0.11 GBP
MCWS 6 273.91 45.65 0.6100 0.0500 1.97 GBP
MCWS Merchandise Rtn 1 46.49 46.49 0.6100 0.0000 0.28 GBP
MC 85 4,467.31 52.56 0.6100 0.0500 31.50 GBP
MC Merchandise Rtn 2 93.40 46.70 0.6100 0.0000 0.57 GBP
MCBS 27 3,302.36 122.31 0.6100 0.0500 21.49 GBP
MCBS Merchandise Rtn 2 120.32 60.16 0.6100 0.0000 0.73 GBP
MCWC 1 152.20 152.20 0.6100 0.0500 0.98 GBP
VISA 3DS
359 24,498.75 68.24 0.0000 0.0300 10.77 GBP
AUTHENTICATION FEE
MCARD 3DS2
400 30,367.70 75.92 0.0000 0.0300 12.00 GBP
AUTHENTICATION FEE
NON SECURE FEE 96 11,138.12 116.02 0.0900 0.0000 10.02 GBP
TOTAL 411.28 GBP"""

    items, stated_total = parse_transactions_charges(sample)

    print(f"Parsed {len(items)} line items\n")
    computed_total = 0.0
    for it in items:
        flag = "" if it["confidence"] in ("CONFIRMED",) else f"  [{it['confidence']}]"
        refund = " (REFUND)" if it["is_refund"] else ""
        print(f"  {it['description']:32s} {it['category']:16s} "
              f"items={it['items']:>4} fee=£{it['fee_amount']:>7.2f}{refund}{flag}")
        computed_total += it["fee_amount"] if not it["is_refund"] else -it["fee_amount"]

    print(f"\nStatement's own stated TOTAL: £{stated_total:.2f}")
    print(f"Parser's computed total:      £{sum(i['fee_amount'] for i in items):.2f}  "
          f"(without refund sign adjustment)")
    naive_sum = sum(i["fee_amount"] for i in items)
    print(f"\nReconciliation check: {'PASS' if abs(naive_sum - stated_total) < 0.01 else 'FAIL'} "
          f"(naive sum of fee_amount column vs stated total)")
