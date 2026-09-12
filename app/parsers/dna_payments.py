"""
DNA Payments parser.
Validated against real statement: Olympus Mobile Tyres Ltd, Jun 2024.

CRITICAL (per Kos): like AIB, DNA Payments never prints a rate anywhere on
this statement - no MSC Rate column at all, in any section. The only way to
get the true cost per category is:

    true_rate = abs(Total Fee) / Volume

Confirmed against every row in the A2 (per payment type) table on the real
statement: true rates ranged from 0.305% to 2.505% depending on card type -
entirely invisible unless you do this division yourself.

SEPARATE FINDING (also per Kos): the "Acquiring" column is NOT the full
percentage-based fee - it's a flat per-transaction charge (an
authorisation-style fee), confirmed as exactly GBP 0.01 x transaction count
on every row of the real A1 (per day) table. "Total" = Acquiring (flat,
per-transaction) + the real scheme/interchange percentage cost. Never treat
"Total" as pure percentage-driven; the per-transaction component must be
identified and subtracted to see the true percentage cost in isolation.

STRUCTURE: this statement always has four deduction sections (A transactional,
B recurring, C non-recurring/one-off, D other merchant fees) - all four exist
as fixed headers every month, even when B/C/D total zero, as they do on this
statement. A real parser must always check all four rather than assume B/C/D
are empty because they were empty last time - a monthly PCI fee or a one-off
charge would land there, not in the daily A1/A2 tables.

Description text in A2 sometimes wraps onto the FOLLOWING line (e.g.
"MasterCard Domestic" on the numeric line, then "Corporate Credit" alone on
the next line before the next numeric row) - handled by carrying forward any
trailing non-numeric line and prepending it to the next row's description.
"""
import re

A1_DAY_ROW = re.compile(
    r"^\s*(?P<date>\d{1,2} \w{3})\s+Processed\s+"
    r"(?P<volume>[\d,]+\.\d{2})\s+(?P<count>\d+)\s+"
    r"(?P<acquiring>-?[\d,]+\.\d{2})\s+(?P<refunds>-?[\d,]+\.\d{2})\s+(?P<total>-?[\d,]+\.\d{2})\s*$"
)
A1_TOTALS_ROW = re.compile(
    r"^\s*Totals\s+(?P<volume>[\d,]+\.\d{2})\s+(?P<count>\d+)\s+"
    r"(?P<acquiring>-?[\d,]+\.\d{2})\s+(?P<refunds>-?[\d,]+\.\d{2})\s+(?P<total>-?[\d,]+\.\d{2})\s*$"
)
# A2: description (possibly empty on a continuation line) followed by the 5 numeric columns.
A2_LINE = re.compile(
    r"^(?P<desc>.*?)\s+(?P<volume>[\d,]+\.\d{2})\s+(?P<count>\d+)\s+"
    r"(?P<acquiring>-?[\d,]+\.\d{2})\s+(?P<refunds>-?[\d,]+\.\d{2})\s+(?P<total>-?[\d,]+\.\d{2})\s*$"
)
BCD_TOTALS_ROW = re.compile(r"^\s*Totals\s+(?P<total>[\d,]+\.\d{2})\s*$")

# A genuine wrapped description continuation on this statement is always a
# short fragment made only of card-category vocabulary (e.g. "Debit",
# "Corporate Credit"). Repeating page-footer/address boilerplate can appear
# as separate lines in the same gap when a table spans a page break (caught
# against the real PDF's own extracted text: both a genuine "Debit" line
# AND an unrelated "London, SW1W 0EN. DNA Payments Limited is authorised..."
# line appeared after the same row) - a marker denylist alone isn't robust
# enough since address text doesn't always contain a fixed marker at the
# start. Instead, only accept a continuation line as real if every word in
# it is drawn from this vocabulary.
DESCRIPTION_CONTINUATION_WORDS = {
    "credit", "debit", "personal", "corporate", "domestic",
    "international", "intra", "commercial", "premium",
}


def _f(s):
    return float(s.replace(",", ""))


def _is_genuine_description_continuation(line):
    words = [w.strip(".,").lower() for w in line.strip().split()]
    return bool(words) and all(w in DESCRIPTION_CONTINUATION_WORDS for w in words)


def parse_a1_daily(lines):
    """Section A1: Transactional fees per day."""
    rows = []
    stated_total = None
    for raw in lines:
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        m = A1_DAY_ROW.match(line)
        if m:
            volume = _f(m.group("volume"))
            acquiring = _f(m.group("acquiring"))
            count = int(m.group("count"))
            total = _f(m.group("total"))
            per_txn_rate = acquiring / count if count else 0.0
            percentage_component = total - acquiring
            rows.append({
                "date": m.group("date"),
                "volume": volume,
                "count": count,
                "acquiring_flat_fee": acquiring,
                "acquiring_per_txn_rate": round(per_txn_rate, 4),
                "refunds_chargebacks": _f(m.group("refunds")),
                "total": total,
                "percentage_component": round(percentage_component, 4),
            })
            continue
        tm = A1_TOTALS_ROW.match(line)
        if tm:
            stated_total = {
                "volume": _f(tm.group("volume")),
                "count": int(tm.group("count")),
                "acquiring": _f(tm.group("acquiring")),
                "total": _f(tm.group("total")),
            }
    return rows, stated_total


def parse_a2_by_type(lines):
    """Section A2: Transactional fees per payment type. Never trusts a printed
    rate (there isn't one on this statement) - always derives true_rate from
    Total / Volume. Row descriptions sometimes wrap: the numeric line carries
    the FIRST part of the description (e.g. "MasterCard Domestic"), and a
    following text-only line carries the rest (e.g. "Corporate Credit"),
    which belongs to the row ALREADY emitted, not the next one."""
    items = []
    stated_total = None
    for raw in lines:
        line = raw.rstrip("\n")
        if not line.strip():
            continue

        if line.strip().startswith("Totals"):
            tm = A2_LINE.match(line)
            if tm:
                stated_total = {
                    "volume": _f(tm.group("volume")),
                    "count": int(tm.group("count")),
                    "total": _f(tm.group("total")),
                }
            continue

        m = A2_LINE.match(line)
        if m:
            desc = m.group("desc").strip()
            volume = _f(m.group("volume"))
            total = _f(m.group("total"))
            true_rate = abs(total) / volume * 100 if volume else None
            items.append({
                "description": desc,
                "volume": volume,
                "count": int(m.group("count")),
                "acquiring_flat_fee": _f(m.group("acquiring")),
                "total": total,
                "true_effective_rate_pct": round(true_rate, 4) if true_rate is not None else None,
            })
        else:
            # Only append if this text-only line is genuinely a wrapped
            # description fragment (card-category vocabulary only) -
            # otherwise it's page-footer/address boilerplate and is
            # discarded rather than glued onto the last row's description.
            if items and _is_genuine_description_continuation(line):
                items[-1]["description"] = (items[-1]["description"] + " " + line.strip()).strip()

    return items, stated_total


def parse_bcd_section(lines):
    """Sections B (Recurring), C (Non-recurring/one-off), D (Other merchant
    fees). Always parsed even when zero - these are fixed statement sections
    that can carry real charges (e.g. a monthly PCI fee) in other months."""
    stated_total = None
    for raw in lines:
        line = raw.rstrip("\n")
        m = BCD_TOTALS_ROW.match(line)
        if m:
            stated_total = _f(m.group("total"))
    return stated_total


if __name__ == "__main__":
    a1_lines = """ 1 Jun            Processed                        1,900.00                         9                   -0.09                      0.00      -8.64
 2 Jun            Processed                        1,480.00                         9                   -0.09                      0.00      -6.08
 3 Jun            Processed                         880.00                          5                   -0.05                      0.00       -3.71
 7 Jun            Processed                       2,205.00                          12                   -0.12                     0.00     -10.46
 21 Jun           Processed                       2,260.00                          12                   -0.12                     0.00      -15.57
 Totals                                       42,968.61                        239                    -2.39                     0.00      -225.24""".splitlines()

    a1_rows, a1_total = parse_a1_daily(a1_lines)
    print(f"A1 (sample of 5 days parsed): {len(a1_rows)} rows")
    for r in a1_rows[:3]:
        print(f"  {r['date']}: volume={r['volume']:.2f} count={r['count']} "
              f"acquiring(flat, per-txn)={r['acquiring_flat_fee']} (=GBP {r['acquiring_per_txn_rate']}/txn) "
              f"percentage-only component={r['percentage_component']}")
    print(f"  Stated totals row: volume={a1_total['volume']}, count={a1_total['count']}, total={a1_total['total']}")

    a2_lines = """MasterCard Domestic                         2,237.00                          9                   -0.09                       0.00        -40.36
Corporate Credit
MasterCard Domestic                         2,340.00                         10                    -0.10                      0.00         -18.82
Corporate Debit
MasterCard Domestic                          7,515.00                        37                   -0.37                       0.00        -45.46
Personal Credit
MasterCard Domestic                         12,306.61                       64                    -0.64                       0.00        -37.56
Personal Debit
MasterCard International                       190.00                          1                   -0.01                      0.00         -4.76
Personal Credit
MasterCard Intra Personal                     295.00                           1                   -0.01                      0.00         -5.32
Debit
Others                                           0.00                        13                    -0.13                      0.00          -0.13
VISA Domestic Corporate                       470.00                          2                   -0.02                       0.00         -8.48
Credit
VISA Domestic Corporate                        510.00                         3                   -0.03                       0.00           -4.11
Debit
VISA Domestic Personal                      2,645.00                         16                    -0.16                      0.00         -16.03
Credit
VISA Domestic Personal                     14,460.00                        83                    -0.83                       0.00         -44.21
Debit
Totals                                    42,968.61                        239                    -2.39                      0.00        -225.24""".splitlines()

    a2_items, a2_total = parse_a2_by_type(a2_lines)
    print(f"\nA2 (by payment type): {len(a2_items)} rows")
    computed_total = 0.0
    for i in a2_items:
        rate_str = f"{i['true_effective_rate_pct']}%" if i["true_effective_rate_pct"] is not None else "n/a (zero volume)"
        print(f"  {i['description']:38s} volume={i['volume']:>10,.2f}  total=GBP {i['total']:>7.2f}  TRUE rate={rate_str}")
        computed_total += i["total"]
    print(f"\nComputed A2 total: GBP {computed_total:.2f}  |  Stated: GBP {a2_total['total']:.2f}  |  "
          f"{'PASS' if abs(computed_total - a2_total['total']) < 0.01 else 'FAIL'}")

    bcd_lines_b = "Totals                                                                                                                                    0.00".splitlines()
    b_total = parse_bcd_section(bcd_lines_b)
    print(f"\nSection B (Recurring) parsed total: GBP {b_total:.2f} (checked, not skipped)")
