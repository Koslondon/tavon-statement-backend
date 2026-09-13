"""
Clover / First Data Fees section parser.
Validated against real statements: Rosemount Hotel, Cavendish French.

The Fees section (F FEES) is a DIFFERENT table from Service Charges (E) with
its own phrasing, even though some patterns look superficially similar:

  1. Percentage rate:   "[DESC] .0NNNNN RATE TIMES [volume] [fee]"
     - note: "RATE TIMES", NOT "DISC RATE TIMES" like Service Charges
  2. Per-transaction:    "[DESC] N TRANSACTIONS AT .0NNNNN [fee]"
     - same phrasing as Service Charges' per-transaction rows (e.g. Authorisation Request)
  3. Trans totaling:      "[DESC] N TRANS TOTALING [volume] [fee]"
     - looks like it should be rate x volume but ISN'T: two real rows with
       the same "9 TRANS TOTALING" shape produced implied rates of 0.045%
       and 0.45% - a 10x difference. No shared/derivable rate exists for
       this row type. Capture count/volume/fee as given; don't compute a rate.
  4. Flat fee, no rate/volume/count at all: "[DESC] [fee]"
     - e.g. Monthly Maintenance Fee, PCI DSS Management Fee, PCI DSS Non Compliance
"""
import re

RATE_ROW = re.compile(
    r"^(?P<desc>.+?)\s+(?P<rate>\.\d+)\s+(?:\w+\s+)?RATE TIMES\s+(?P<volume>[\d,]+\.\d{2})\s+(?P<fee>-?[\d,]+\.\d{2})\s*$"
)
# Chain-format fallback: same rate-based fee, but no volume printed at all -
# confirmed on a real Alternative Salon Ltd statement's "VISA INT
# ACCEPTANCE FEE .004500 RATE TIMES   -0.36" line. Per Kos: recoverable as
# fee / rate, same principle as clover.py's PCT_ROW_NO_VOLUME.
RATE_ROW_NO_VOLUME = re.compile(
    r"^(?P<desc>.+?)\s+(?P<rate>\.\d+)\s+(?:\w+\s+)?RATE(?:\s+TIMES)?\s+(?P<fee>-?[\d,]+\.\d{2})\s*$"
)
PER_TXN_ROW = re.compile(
    r"^(?P<desc>.+?)\s+(?P<count>\d+)\s+TRANSACTIONS? AT\s+(?P<rate>\.\d+)\s+(?P<fee>-?[\d,]+\.\d{2})\s*$"
)
# Chain-format fallback: same per-transaction fee, but the per-transaction
# rate itself isn't printed at all (only the count and the total fee) -
# confirmed on a real Alternative Salon Ltd statement's "AUTHORISATION
# REQUEST 194 TRANSACTIONS AT   -9.66" line. Per Kos: recoverable as
# fee / count.
PER_TXN_ROW_NO_RATE = re.compile(
    r"^(?P<desc>.+?)\s+(?P<count>\d+)\s+TRANSACTIONS? AT\s+(?P<fee>-?[\d,]+\.\d{2})\s*$"
)
TRANS_TOTALING_ROW = re.compile(
    r"^(?P<desc>.+?)\s+(?P<count>\d+)\s+TRANS TOTALING\s+(?P<volume>[\d,]+\.\d{2})\s+(?P<fee>-?[\d,]+\.\d{2})\s*$"
)
# Real lines carry a leading date ("30/04/22  MONTHLY MAINTENANCE FEE  -3.99")
# that a description charset restricted to [A-Z0-9 /.&+-] would reject outright.
FLAT_FEE_ROW = re.compile(r"^(?P<desc>.+?)\s+(?P<fee>-?[\d,]+\.\d{2})\s*$")
TOTAL_ROW = re.compile(r"^Total\s+(-?[\d,]+\.\d{2})\s*$")
# Chain statements prefix every row with a Merchant Number column before the
# date; regular Outlet statements just have the date. Strip whichever is
# present before matching, same fix as clover.py's service-charges parser.
ROW_PREFIX = re.compile(r"^(?:\d{9,}\s+)?\d{2}/\d{2}/\d{2}\s+")


def _f(s):
    return float(s.replace(",", ""))


def _is_section_header(line, name):
    # Some statements print a leading single-letter section marker (e.g.
    # "F      FEES" instead of a bare "FEES") - tolerate it, same issue
    # confirmed on a real Cavendish French statement for Service Charges.
    return re.match(r"^[A-Z]?\s*" + re.escape(name) + r"$", line) is not None


def parse_fees(lines):
    """Section-anchored for the same reason as clover.py's
    parse_service_charges: PER_TXN_ROW's shape is shared with Service
    Charges rows (e.g. "REFUND TXN CHARGE 5 TRANSACTIONS AT .500000"), so
    an unanchored scan of the whole statement would double-count those as
    Fees rows too. Only lines between the "FEES" header and that section's
    own "Total" line are considered."""
    items = []
    stated_total = None
    in_section = False
    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        if _is_section_header(line, "FEES"):
            in_section = True
            continue

        if not in_section:
            continue

        line = ROW_PREFIX.sub("", line)

        tm = TOTAL_ROW.match(line)
        if tm:
            stated_total = _f(tm.group(1))
            break  # this section's own closing total - stop here

        # Order matters: try the more specific patterns before the generic
        # flat-fee fallback, since a flat fee row is just "[text] [amount]"
        # and would otherwise swallow every other row type too.
        m = RATE_ROW.match(line)
        if m:
            rate = float(m.group("rate"))
            volume = _f(m.group("volume"))
            fee = _f(m.group("fee"))
            computed = rate * volume
            scale_flag = None
            if abs(computed - abs(fee)) > 0.02:
                if abs(computed * 100 - abs(fee)) < 0.02:
                    scale_flag = "rate appears to need x100 correction"
                elif abs(computed / 100 - abs(fee)) < 0.02:
                    scale_flag = "rate appears to need /100 correction"
                else:
                    scale_flag = "rate x volume does not match stated fee at any x100 scale"
            items.append({
                "description": m.group("desc").strip(),
                "row_type": "percentage",
                "rate": rate, "volume": volume, "fee": fee,
                "scale_flag": scale_flag,
            })
            continue

        m = RATE_ROW_NO_VOLUME.match(line)
        if m:
            rate = float(m.group("rate"))
            fee = _f(m.group("fee"))
            derived_volume = round(abs(fee) / rate, 2) if rate else None
            items.append({
                "description": m.group("desc").strip(),
                "row_type": "percentage",
                "rate": rate,
                "volume": derived_volume,
                "volume_derived": derived_volume is not None,
                "fee": fee,
                "scale_flag": None,
            })
            continue

        m = PER_TXN_ROW.match(line)
        if m:
            items.append({
                "description": m.group("desc").strip(),
                "row_type": "per_transaction",
                "count": int(m.group("count")),
                "per_txn_rate": float(m.group("rate")),
                "fee": _f(m.group("fee")),
            })
            continue

        m = PER_TXN_ROW_NO_RATE.match(line)
        if m:
            count = int(m.group("count"))
            fee = _f(m.group("fee"))
            items.append({
                "description": m.group("desc").strip(),
                "row_type": "per_transaction",
                "count": count,
                "per_txn_rate": round(abs(fee) / count, 4) if count else None,
                "per_txn_rate_derived": True,
                "fee": fee,
            })
            continue

        m = TRANS_TOTALING_ROW.match(line)
        if m:
            items.append({
                "description": m.group("desc").strip(),
                "row_type": "trans_totaling_opaque",
                "count": int(m.group("count")),
                "volume": _f(m.group("volume")),
                "fee": _f(m.group("fee")),
                "note": "rate not derivable from this row shape - do not compute one",
            })
            continue

        m = FLAT_FEE_ROW.match(line)
        if m:
            items.append({
                "description": m.group("desc").strip(),
                "row_type": "flat_fee",
                "fee": _f(m.group("fee")),
            })
            continue

    return items, stated_total


if __name__ == "__main__":
    rosemount_fees = """AUTHORISATION REQUEST 250 TRANSACTIONS AT .039500 -9.88
MONTHLY MAINTENANCE FEE -3.99
PCI DSS MANAGEMENT FEE -4.99
PCI DSS NON COMPLIANCE -35.00
VISA UK & EU E-COMM/MOTO FEE .000100 RATE TIMES 11,216.70 -1.12
VISA INT E-COMM/MOTO FEE .005500 RATE TIMES 2,445.95 -13.45
M/C+MAESTRO EU ACCEPTANCE FEE 9 TRANS TOTALING 970.70 -0.44
M/C INT. RETAIL ACCEPTANCE FEE 9 TRANS TOTALING 719.15 -3.24
VISA INT ACCEPTANCE FEE .004500 RATE TIMES 2,756.65 -12.40
Total -84.51""".splitlines()

    cavendish_fees = """AUTHORISATION REQUEST 55 TRANSACTIONS AT .020000 -1.10
VISA UK & EU E-COMM/MOTO FEE .000100 RATE TIMES 1,126.87 -0.11
Total -1.21""".splitlines()

    for name, lines in [("Rosemount Hotel", rosemount_fees), ("Cavendish French", cavendish_fees)]:
        items, stated_total = parse_fees(lines)
        by_type = {}
        for i in items:
            by_type.setdefault(i["row_type"], 0)
            by_type[i["row_type"]] += 1
        computed_total = sum(i["fee"] for i in items)
        print(f"=== {name} ===")
        print(f"  Parsed {len(items)} rows: {by_type}")
        print(f"  Computed total: £{computed_total:.2f}  |  Stated: £{stated_total:.2f}  |  "
              f"{'PASS' if abs(computed_total - stated_total) < 0.01 else 'FAIL'}")
        for i in items:
            if i.get("scale_flag"):
                print(f"    FLAGGED: {i['description']} - {i['scale_flag']}")
        print()
