"""
EVO Payments International parser.
Validated against real statement: PATON OF WALTON LIMITED, merchant number
GB0000000222673, period 01-08-2026 to 31-08-2026.

BLENDED RATE: never stated directly anywhere on the statement. Per
Kos's own instruction: take the total MSC fee from the Daily
Settlement Report's own "Total" line, divide by that same line's Total
Sales figure, multiply by 100. That line is the single most
authoritative figure on the whole 13-page document.

PER-CATEGORY BREAKDOWN: the "Monthly activity for Visa and MasterCard"
table lists 12 card-type rows, but their text labels wrap across
multiple lines in a genuinely inconsistent way (confirmed directly
against the real extracted text - some rows keep a fragment of their
label on the same line as the numbers, some don't), making the label
text itself unreliable to parse. The 12 rows always appear in the same
fixed order, though, matching the statement's own fixed
Credit/Debit/Other grouping - so each row is identified by its
position in that known sequence instead, the same technique InterCard's
parser uses with its fee codes when free-text descriptions aren't a
safe key.

Of those 12, 7 map onto this project's standard categories with real
confidence - they sit under the statement's own explicit "Credit" and
"Debit" section headers:
  - Visa Credit, MasterCard Credit                  -> credit
  - MasterCard Commercial Credit                    -> business_credit
  - Visa Debit, MasterCard Debit/Maestro             -> debit
  - Visa Business Debit, MasterCard Commercial Debit -> business_debit

The remaining 5 sit under the statement's own separate "Other" /
"MasterCard Premium" grouping - Visa Corporate, Visa Business,
MasterCard World Signia, MasterCard World, MasterCard Corporate. These
are genuinely ambiguous (a "World" card is a premium personal
rewards tier, not obviously a business product the way "Corporate"
is) - rather than guess, they're left uncategorised and shown
individually, same principle as AIB and DNA Payments: never silently
guess a fee into the wrong bucket, show it under its own statement
wording instead.

Reconciliation note: the 12 rows' "Value in £" sum to the Daily
Settlement Report's NET Settlement figure (£73,841.27), not its GROSS
Total Sales figure (£73,920.40) - the £79.13 difference is exactly
the period's refunds, already netted out of the per-row table but not
the Total Sales line. Expected, not a parsing error; true_total_fees
and the blended rate still use the Daily Settlement Report's own
authoritative gross turnover, per Kos's instruction.

TRANSACTION COUNT / ATV: summed across all 12 rows (237 on this
statement) - the Daily Settlement Report doesn't carry a transaction
count itself, only the per-card-type table does.

Amex: billed separately by Amex directly ("passed to the card issuer
to settle and bill separately"), so no EVO-charged fee applies - kept
entirely separate from the Visa/Mastercard blended rate, surfaced only
for the mix breakdown.

Number format throughout is European style: comma as the decimal
point, a plain space as the thousands separator (e.g. "73 920,40" =
seventy-three thousand, nine hundred and twenty pounds, forty pence).
Confirmed as a regular space character (0x20), not a non-breaking
space, checked directly against the real extracted bytes.

KNOWN GAP: no domestic/international split anywhere on this statement
- the detailed table has no International row at all, and the one
real statement available to validate against has zero DCC activity
too (the column exists, entirely 0,00 throughout). A future EVO
statement with real figures in either needs this extending, not
assumed to already be handled - this is exactly why the Statement
Checker now has a manual ratio override, so a seller can still supply
a known domestic/international split by hand when EVO's own statement
can't provide one.
"""
import re

NUM = r"-?[\d\s]+,\d{2}"

# Fixed order the 12 rows always appear in - confirmed directly against
# the real statement's own row sequence.
ROW_SEQUENCE = [
    ("Visa Credit", "credit"),
    ("MasterCard Credit", "credit"),
    ("MasterCard Commercial Credit", "business_credit"),
    ("Visa Debit", "debit"),
    ("Visa Business Debit", "business_debit"),
    ("MasterCard Debit/Maestro", "debit"),
    ("MasterCard Commercial Debit", "business_debit"),
    ("Visa Corporate", None),
    ("Visa Business", None),
    ("MasterCard World Signia", None),
    ("MasterCard World", None),
    ("MasterCard Corporate", None),
]


def _f(s):
    """Converts EVO's "73 920,40" / "-79,13" style numbers to float.
    Strips all whitespace (not just spaces) - some rows carry a
    leading newline fragment from the label-wrapping quirk above."""
    return float(re.sub(r"\s", "", s).replace(",", "."))


def _section(text, heading):
    """Grabs everything from a section heading up to the next page-footer
    marker - sections are clearly delimited this way throughout."""
    m = re.search(re.escape(heading) + r"(.*?)(?=\n\s*Page \d+ of \d+)", text, re.S)
    return m.group(1) if m else None


def parse_card_type_rows(text):
    """The 12-row Visa/Mastercard breakdown table. Returns a list of
    dicts with description, category (None if genuinely ambiguous),
    volume, count, and fee (Variable MSC - the only non-zero fee
    component on this statement; Fixed MSC/Margin/Scheme Fees/
    Interchange are all 0,00 throughout, apparently folded into
    Variable MSC for this merchant)."""
    section = _section(text, "Monthly activity for Visa and MasterCard")
    if not section:
        return []
    row_re = re.compile(
        r"^.*?(" + NUM + r")\s+(\d+)\s+(" + NUM + r")\s+(" + NUM + r")\s+"
        r"(" + NUM + r")\s+(" + NUM + r")\s+(" + NUM + r")\s*$",
        re.M,
    )
    matches = row_re.findall(section)
    rows = []
    for i, m in enumerate(matches):
        if i >= len(ROW_SEQUENCE):
            break  # more numeric rows than known card types - don't guess past the known sequence
        desc, category = ROW_SEQUENCE[i]
        rows.append({
            "description": desc,
            "category": category,
            "volume": _f(m[0]),
            "count": int(m[1]),
            "fee": _f(m[3]),  # Variable MSC
        })
    return rows


def parse_daily_settlement_total(text):
    """The Daily Settlement Report's own Total line. Returns
    (turnover, total_msc) or (None, None) if not found."""
    section = _section(text, "Daily Settlement Report")
    if not section:
        return None, None
    m = re.search(
        r"^Total\s+(" + NUM + r")\s+(" + NUM + r")\s+(" + NUM + r")\s+"
        r"(" + NUM + r")\s+(" + NUM + r")\s+(" + NUM + r")\s+(" + NUM + r")\s+(" + NUM + r")\s*$",
        section, re.M,
    )
    if not m:
        return None, None
    turnover = _f(m.group(1))
    total_msc = _f(m.group(3))
    return turnover, total_msc


def parse_other_fees_total(text):
    """The smaller Authorisation/Refund fees total - sits alongside the
    MSC and is not included in it. Returns a positive float (the
    statement shows it negative, as a deduction)."""
    section = _section(text, "Monthly activity for other fees")
    if not section:
        return None
    m = re.search(r"Total\s+(" + NUM + r")\s*$", section, re.M)
    return abs(_f(m.group(1))) if m else None


def parse_amex(text):
    """Amex turnover and transaction count - billed separately by Amex
    directly, so no EVO-charged fee applies to it. Returns
    (count, turnover) or (None, None) if no Amex activity this period."""
    section = _section(text, "Monthly activity for additional card types")
    if not section:
        return None, None
    m = re.search(r"American Express\s+(\d+)\s+(" + NUM + r")\s*$", section, re.M)
    if not m:
        return None, None
    return int(m.group(1)), _f(m.group(2))


def parse_evo_statement(text):
    turnover, total_msc = parse_daily_settlement_total(text)
    other_fees = parse_other_fees_total(text)
    amex_count, amex_turnover = parse_amex(text)
    rows = parse_card_type_rows(text)
    transaction_count = sum(r["count"] for r in rows) or None
    true_total_fees = None
    if total_msc is not None:
        true_total_fees = total_msc + (other_fees or 0)
    return {
        "turnover": turnover,
        "total_msc": total_msc,
        "other_fees": other_fees,
        "true_total_fees": round(true_total_fees, 2) if true_total_fees is not None else None,
        "transaction_count": transaction_count,
        "rows": rows,
        "amex_transaction_count": amex_count,
        "amex_turnover": amex_turnover,
    }


if __name__ == "__main__":
    import subprocess, sys
    pdf_path = sys.argv[1] if len(sys.argv) > 1 else "/mnt/user-data/uploads/evo_cards.pdf"
    text = subprocess.run(["pdftotext", "-layout", pdf_path, "-"], capture_output=True, text=True).stdout
    result = parse_evo_statement(text)
    print(f"Turnover (Visa+MC, gross): £{result['turnover']:,.2f}")
    print(f"True total fees:           £{result['true_total_fees']:,.2f}")
    if result["turnover"] and result["true_total_fees"] is not None:
        print(f"Blended rate:               {result['true_total_fees'] / result['turnover'] * 100:.4f}%")
    print(f"Transaction count:          {result['transaction_count']}")
    if result["transaction_count"]:
        print(f"ATV:                        £{result['turnover'] / result['transaction_count']:.2f}")
    print(f"\n{len(result['rows'])} card-type rows:")
    rows_total = 0
    for r in result["rows"]:
        rate = r["fee"] / r["volume"] * 100 if r["volume"] else 0
        rows_total += r["volume"]
        print(f"  {r['description']:30s} cat={str(r['category']):16s} vol=£{r['volume']:>10,.2f}  "
              f"count={r['count']:>4d}  fee=£{r['fee']:>7.2f}  rate={rate:.3f}%")
    print(f"\nRows sum to £{rows_total:,.2f} (expect Daily Settlement Report's NET £73,841.27 - refunds difference is expected)")
    if result["amex_turnover"]:
        print(f"\nAmex turnover: £{result['amex_turnover']:,.2f} ({result['amex_transaction_count']} transactions)")
