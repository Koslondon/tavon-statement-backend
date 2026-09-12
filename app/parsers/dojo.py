"""
Dojo (Paymentsense Limited) statement parser.
Validated against 2 real statements with GENUINELY DIFFERENT invoice
templates - not just formatting variance:

  - Union Hair (Oct 2024): simpler template. Three flat fee-style tables
    ("Card transaction fees", "Card transaction rates",
    "Card machine & account services"), each closing with its own "Total"
    line.
  - Mba Best Ltd (Jul 2024): detailed template. A "Card transaction rates
    breakdown" table with many more card-type rows (including EEA and
    International variants), closing with "Subtotal" then a separate
    "Total transaction charges" confirmation line.

Both templates share the same underlying row shape for the rate
breakdown table, which is the one that matters for a true blended rate:

    <card type desc>  <count>  £<volume>  <rate>  £<total>  <vat code>

`<rate>` comes in two real forms:
  - plain percentage:      "1.40%"
  - blended (EEA/Intl):    "1.35% + £0.13"   (percentage PLUS a flat
                            per-transaction component - confirmed on
                            Mba Best's International/EEA rows)

One further real wrap case (Mba Best): a long description can split
across a comma, with the numeric row landing in between and the
remainder of the description on its own line AFTER the numbers, e.g.:

    Mastercard Corporate and Purchasing,
    1            £12.00     3.45% + £0.13     £0.54   E
    International

This is handled the same way as Global Payments' prefix/suffix wrap:
buffer a bare comma-ending text line, then peek one line past a matched
numeric-only row for a plain-text (no digits) completion.
"""
import re

RATE_ROW = re.compile(
    r"^(?P<desc>.+?),?\s+"
    r"(?P<count>\d[\d,]*)\s+"
    r"£(?P<volume>[\d,]+\.\d{2})\s+"
    r"(?P<rate>[\d.]+%(?:\s*\+\s*£[\d.]+)?)\s+"
    r"£(?P<total>[\d,]+\.\d{2})\s+"
    r"(?P<vat>[A-Za-z]+)\s*$"
)
# Same shape, but with no leading description - used for the wrap case.
RATE_ROW_NUMERIC_ONLY = re.compile(
    r"^(?P<count>\d[\d,]*)\s+"
    r"£(?P<volume>[\d,]+\.\d{2})\s+"
    r"(?P<rate>[\d.]+%(?:\s*\+\s*£[\d.]+)?)\s+"
    r"£(?P<total>[\d,]+\.\d{2})\s+"
    r"(?P<vat>[A-Za-z]+)\s*$"
)
# Closing line for the rate breakdown table - "Total" (Union Hair) or
# "Subtotal" (Mba Best), same 3-number shape either way.
RATE_TABLE_CLOSE = re.compile(
    r"^(?:Total|Subtotal)\s+(?P<count>\d[\d,]*)\s+"
    r"£(?P<volume>[\d,]+\.\d{2})\s+£(?P<total>[\d,]+\.\d{2})\s*$"
)

# Union Hair's simpler fee-style tables ("Card transaction fees",
# "Card machine & account services"): desc, count, unit price, total, vat%.
FLAT_FEE_ROW = re.compile(
    r"^(?P<desc>.+?)\s+(?P<count>\d[\d,]*)\s+£(?P<unit_price>[\d,]+\.\d{2})\s+"
    r"£(?P<total>[\d,]+\.\d{2})\s+(?P<vat>\d+%)\s*$"
)

# Known top-level summary labels, shared (with slightly different sets)
# across both templates - matched by exact label text so this can't
# accidentally grab an unrelated "<text> £X.XX" line elsewhere.
SUMMARY_LABELS = [
    "Card transaction fees", "Card transaction rates", "Additional rates",
    "Card machine & account services", "Net amount", "VAT total", "Total due",
    "Card transactions", "Card machine services", "VAT",
]


def _f(s):
    return float(s.replace(",", "").replace("£", ""))


def parse_summary(lines):
    """Top-level invoice summary - present in some form in both templates.

    Uses a search (not startswith) for the label: Union Hair's PDF has a
    two-column layout at the top, and pdftotext -layout merges left-column
    text ("You don't need to do a thing...") onto the same line as some
    summary labels on the right - confirmed this silently dropped "Card
    transaction rates" and "Additional rates" when matched by prefix only."""
    summary = {}
    pattern = re.compile(
        r"(" + "|".join(re.escape(l) for l in SUMMARY_LABELS) + r")\s+£([\d,]+\.\d{2})\s*$"
    )
    for raw in lines:
        line = raw.strip()
        m = pattern.search(line)
        if m:
            summary[m.group(1)] = _f(m.group(2))
    return summary


def parse_rate_breakdown(lines):
    """The 'Card transaction rates' (Union Hair) or 'Card transaction
    rates breakdown' (Mba Best) table - the true per-card-type rate data.
    Returns (items, stated_total) where stated_total comes from the
    table's own closing Total/Subtotal line."""
    items = []
    stated = None
    pending_prefix = None
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line:
            continue

        close = RATE_TABLE_CLOSE.match(line)
        if close:
            stated = {
                "count": int(close.group("count").replace(",", "")),
                "volume": _f(close.group("volume")),
                "total": _f(close.group("total")),
            }
            break

        m = RATE_ROW.match(line)
        if m:
            pending_prefix = None
            items.append(_row_from_match(m))
            continue

        # Wrap case: bare description fragment ending in a comma, numbers
        # on the next line, description continuation possibly after that.
        if line.endswith(",") and not re.search(r"\d", line):
            pending_prefix = line
            continue

        if pending_prefix is not None:
            m2 = RATE_ROW_NUMERIC_ONLY.match(line)
            if m2:
                desc = pending_prefix
                if i < len(lines):
                    nxt = lines[i].strip()
                    if nxt and not re.search(r"\d", nxt) and not nxt.startswith(("Page", "Card", "Total", "Subtotal")):
                        desc = f"{pending_prefix} {nxt}"
                        i += 1
                items.append(_row_from_match(m2, desc_override=desc))
                pending_prefix = None
                continue
            pending_prefix = None
        # else: header/blank/page-furniture line - ignore.

    return items, stated


def _row_from_match(m, desc_override=None):
    rate_str = m.group("rate")
    pct_match = re.match(r"^([\d.]+)%", rate_str)
    pct = float(pct_match.group(1)) if pct_match else None
    flat_match = re.search(r"£([\d.]+)$", rate_str)
    flat_component = float(flat_match.group(1)) if flat_match else 0.0
    desc = (desc_override if desc_override is not None else m.group("desc")).strip()
    return {
        "description": desc,
        "count": int(m.group("count").replace(",", "")),
        "volume": _f(m.group("volume")),
        "rate_display": rate_str,
        "percent_component": pct,
        "flat_component": flat_component,
        "total": _f(m.group("total")),
        "vat_code": m.group("vat"),
    }


def parse_flat_fee_table(lines, section_header):
    """Union Hair's simpler tables: 'Card transaction fees' and
    'Card machine & account services'. Section-anchored on the header
    text so it never reads rows from a different table."""
    items = []
    stated_total = None
    in_section = False
    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        if line == section_header:
            in_section = True
            continue

        if not in_section:
            continue

        if line.startswith("Total"):
            m = re.match(r"^Total\s+(?:\d[\d,]*\s+)?£(?P<total>[\d,]+\.\d{2})\s*$", line)
            if m:
                stated_total = _f(m.group("total"))
            break

        m = FLAT_FEE_ROW.match(line)
        if m:
            items.append({
                "description": m.group("desc").strip(),
                "count": int(m.group("count").replace(",", "")),
                "unit_price": _f(m.group("unit_price")),
                "total": _f(m.group("total")),
                "vat_pct": m.group("vat"),
            })

    return items, stated_total


def parse_statement(raw_text):
    """Top-level entry point: returns everything reconciled together."""
    lines = raw_text.splitlines()
    summary = parse_summary(lines)
    rate_items, rate_stated = parse_rate_breakdown(lines)

    rate_computed_total = round(sum(i["total"] for i in rate_items), 2)
    rate_computed_volume = round(sum(i["volume"] for i in rate_items), 2)
    true_blended_rate = (
        round(rate_computed_total / rate_computed_volume * 100, 4)
        if rate_computed_volume else None
    )

    return {
        "summary": summary,
        "rate_breakdown": rate_items,
        "rate_breakdown_computed_total": rate_computed_total,
        "rate_breakdown_stated": rate_stated,
        "true_blended_rate_pct": true_blended_rate,
    }
