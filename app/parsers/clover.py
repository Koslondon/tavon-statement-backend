"""
Clover / First Data (Fiserv) Service Charges + Fees parser.
Validated against 4 real statements: Cavendish French, Alternative Salon,
Motorfix, Rosemount Hotel.

Key gotcha (per Kos, confirmed against real data): Service Charges mixes two
row types that look structurally similar but mean different things:
  1. "[DESC] .0NNNNN DISC RATE TIMES [volume]"   -> a PERCENTAGE rate x volume
  2. "[DESC] N TRANSACTIONS AT .0NNNNN"          -> a flat PENCE-per-transaction fee
Authorisation Request is a separate fee living in the FEES section, not
Service Charges, even though it uses the same "N TRANSACTIONS AT X" shape.

Also builds in a defensive check for the x100 rate-scaling issue Kos flagged
for Clover/First Data statements generally (not observed in this specific
batch, but cheap to guard against): if rate x volume is off from the stated
fee by roughly 100x, flag it rather than silently trust either number.
"""
import re

PCT_ROW = re.compile(
    r"^(?P<desc>.+?)\s+(?P<rate>\.\d+)\s+(?:\w+\s+)?DISC RATE TIMES\s+(?P<volume>[\d,]+\.\d{2})\s+(?P<fee>-?[\d,]+\.\d{2})\s*$"
)
# Chain-format statements (multiple outlets under one merchant) omit the
# volume figure from this table entirely - confirmed on a real Alternative
# Salon Ltd statement, where every row is "[desc] .0NNNNN DISC RATE[ TIMES]?
# [fee]" with no volume number at all between the rate and the fee. Only
# tried after PCT_ROW fails, since PCT_ROW is the more specific/informative
# match when a volume is actually present.
PCT_ROW_NO_VOLUME = re.compile(
    r"^(?P<desc>.+?)\s+(?P<rate>\.\d+)\s+DISC RATE(?:\s+TIMES)?\s+(?P<fee>-?[\d,]+\.\d{2})\s*$"
)
# Confirmed on the real statement: occasionally the row wraps mid-sentence,
# e.g. "MASTERCARD CHIP SERVICE CHARGE .015696 DISC RATE TIMES" on one line
# and "30/04/22  249.60  -3.92" (volume + fee, with its own date prefix) on
# the next. This anchors the wrap point so it can be recombined before retrying PCT_ROW.
PCT_ROW_WRAP_PREFIX = re.compile(
    r"^(?P<desc>.+?)\s+(?P<rate>\.\d+)\s+(?:\w+\s+)?DISC RATE TIMES\s*$"
)
DATE_PREFIX = re.compile(r"^\d{2}/\d{2}/\d{2}\s+")
# Chain statements prefix every row with a Merchant Number column before the
# date (e.g. "520334508559614    30/06/24     MC DEBIT CHIP..."); regular
# Outlet statements just have the date. Strip whichever is present so the
# description capture group never swallows it - previously this caused every
# single row to categorise as "unmapped" even though the fee totals were
# already correct, since the category lookup table has no entries with a
# leading date/merchant-number baked in.
ROW_PREFIX = re.compile(r"^(?:\d{9,}\s+)?\d{2}/\d{2}/\d{2}\s+")
PER_TXN_ROW = re.compile(
    r"^(?P<desc>.+?)\s+(?P<count>\d+)\s+TRANSACTIONS? AT\s+(?P<rate>\.\d+)\s+(?P<fee>-?[\d,]+\.\d{2})\s*$"
)
TOTAL_ROW = re.compile(r"^Total\s+(-?[\d,]+\.\d{2})\s*$")
# Interchange Charges rows are simpler than Service Charges - just a date,
# description, and amount, no rate or volume printed at all.
FLAT_ROW = re.compile(r"^(?P<desc>.+?)\s+(?P<fee>-?[\d,]+\.\d{2})\s*$")
# Section headers sometimes carry a leading single-letter marker from the
# statement's own lettered index (e.g. "E      SERVICE CHARGES" instead of
# a bare "SERVICE CHARGES") - confirmed on a real Cavendish French
# statement. An exact-match check against the bare header silently matched
# nothing, so the whole section was skipped even though its rows and total
# were sitting right there in the text.
def _is_section_header(line, name):
    return re.match(r"^[A-Z]?\s*" + re.escape(name) + r"$", line) is not None


CARD_TYPE_CATEGORY = {
    "MC DEBIT": "debit", "MC DEBIT CHIP": "debit", "MC DEBIT NQ": "debit", "MC DEBIT CHIP NQ": "debit",
    "MC DBT CHP NQ": "debit", "MC DBT CHP": "debit",
    "VISA": "credit", "VISA CHIP": "credit", "VISA NON-QUAL": "credit",
    "MASTERCARD": "credit", "MASTERCARD CHIP": "credit", "MASTERCARD NQ": "credit",
    "MASTERCARD CHIP NQ": "credit",
    "VISA DEBIT": "debit", "VISA DEBIT CHIP": "debit", "VISA NQ DEBIT": "debit",
    "VISA DEBIT NQ": "debit", "VISA DR CHIP": "debit", "VISA DR": "debit",
    "VISA BUS DR CARD": "business_debit", "VISA BUS DR CARD NQ": "business_debit",
    "VISA PRCH": "business_credit",  # (EX BUS DR) -> purchasing, not the debit-card variant
    "MC PURCHASE CARD": "business_credit", "MC PURCHASE CARD NQ": "business_credit",
    "INTL MSTRO": "international",
    "REFUND TXN CHARGE": "refund_fee",  # not a card-type fee, flag separately
}


def _keyword_categorize(key):
    """Fallback for description phrasing not in CARD_TYPE_CATEGORY - keyword
    rules per Kos, so a new abbreviation variant on a future statement gets
    a reasonable bucket instead of silently falling into "unmapped". Tokens
    are checked as whole words (via regex \\b) so e.g. "CORP" in "CORPORATE"
    doesn't accidentally match a shorter unrelated token.
    NOTE: any "(EX BUS DR)" qualifier must already be stripped from `key`
    before calling this - it means "excluding business debit", a negation,
    not a positive business+debit signal, and would otherwise be
    misclassified as business_debit by this same keyword logic.
    """
    def has(*words):
        return any(re.search(r"\b" + w + r"\b", key) for w in words)

    is_business = has("BUS", "BUSINESS", "PRCH", "PURCHASE", "CORP", "CORPORATE", "COMCD")
    is_debit = has("DR", "DEBIT", "DBT", "DB")
    is_visa = has("VISA", "VI")
    is_mc = has("MC", "MASTERCARD", "M/C")

    if is_business:
        return "business_debit" if is_debit else "business_credit"
    if is_debit:
        return "debit"
    if is_visa or is_mc:
        return "credit"
    return "unmapped"


def _f(s):
    return float(s.replace(",", ""))



def _categorize(desc):
    # "(EX BUS DR)" means "excluding business debit cards" - a negation
    # qualifier on a business/purchasing-card row, not a positive signal
    # that this row IS a business debit card. Strip it before any keyword
    # matching, or the fallback heuristic below would wrongly read "BUS"
    # and "DR" together as business_debit.
    key = desc.replace("(EX BUS DR)", "").strip()
    # Strip known trailing labels to find the card-type key
    for suffix in [" NQ SRV CHG", " NQ SRV CHRG", " NQ SERVICE CHARGE", " NQ SERVICE CHRG",
                   " SERVICE CHARGE", " SERV CHRG", " SRV CHG", " SRV CHRG",
                   " NQ SALES TRANS FEE", " NQ SALE T/FEE", " NQ SALES T/FEE",
                   " NQ SALES TRANS", " NQ SALE TRANS",
                   " SALES TRANS FEE", " SALE T/FEE", " SALES T/FEE",
                   " SLS T/FEE", " SALE TRANS FEE"]:
        if key.endswith(suffix):
            key = key[: -len(suffix)].strip()
            break
    if key in CARD_TYPE_CATEGORY:
        return CARD_TYPE_CATEGORY[key]
    return _keyword_categorize(key)


SUMMARY_LINE = re.compile(
    r"Page\s+\d+\s+(?:[A-Z]\s+)?"
    r"(Total Amount Submitted|Interchange Charges|Service Charges|Fees|Chargebacks/Reversals)"
    r"\s+(-?[\d,]+\.\d{2})"
)


CARD_TYPE_TOTAL_ROW = re.compile(
    r"^Total\s+(?P<sales_items>\d+)\s+(?P<sales_amount>[\d,]+\.\d{2})\s+"
    r"(?P<refund_items>\d+)\s+(?P<refund_amount>[\d,]+\.\d{2})\s+"
    r"(?P<net_items>\d+)\s+(?P<net_amount>[\d,]+\.\d{2})\s*$"
)


def parse_transaction_count(lines):
    """Pulls the Net "Total Items" figure from the OUTLET/CHAIN SUMMARY BY
    CARD TYPE table's own Total row (e.g. 228 on the real Anne Urry
    statement). This row's shape - three (items, amount) pairs in a row -
    is distinctive enough to match without section-anchoring: no other
    "Total" line on these statements has three leading integer counts."""
    for raw in lines:
        m = CARD_TYPE_TOTAL_ROW.match(raw.strip())
        if m:
            return int(m.group("net_items"))
    return None


def parse_summary(text):
    """Pulls the headline figures from the OUTLET/CHAIN SUMMARY box on page
    1 - "Total Amount Submitted" (turnover) plus the stated totals for
    Interchange Charges, Service Charges, Fees, and Chargebacks/Reversals.
    Confirmed against 4 real statements spanning both Outlet and Chain
    statement layouts, with and without a lettered section-index marker.
    Takes the full statement text (not per-line), since this box's layout
    varies enough between statements that matching across the whole text
    is more robust than trying to anchor to one exact line shape."""
    result = {}
    label_to_key = {
        "Total Amount Submitted": "turnover",
        "Interchange Charges": "interchange_stated",
        "Service Charges": "service_charges_stated",
        "Fees": "fees_stated",
        "Chargebacks/Reversals": "chargebacks_stated",
    }
    for label, amount in SUMMARY_LINE.findall(text):
        result[label_to_key[label]] = _f(amount)
    return result


def parse_interchange_charges(lines):
    """Interchange Charges is its own section, present on some statements
    (confirmed on real Cavendish French and Goldcrest Oil statements) and
    absent/all-zero on others. Rows here are simpler than Service Charges -
    just a date, description, and amount, no rate or volume printed at all.
    Categorised into the same card-type buckets so interchange folds into
    the same true-cost-per-card-type view as the service charges."""
    items = []
    stated_total = None
    in_section = False
    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        if _is_section_header(line, "INTERCHANGE CHARGES"):
            in_section = True
            continue

        if not in_section:
            continue

        line = ROW_PREFIX.sub("", line)

        tm = TOTAL_ROW.match(line)
        if tm:
            stated_total = _f(tm.group(1))
            break

        m = FLAT_ROW.match(line)
        if m:
            desc = m.group("desc").strip()
            items.append({
                "description": desc,
                "fee": _f(m.group("fee")),
                "category": _categorize(desc),
            })
            continue

    return items, stated_total


def parse_service_charges(lines):
    """Returns (items, stated_total). Each item tagged with row_type:
    'percentage' or 'per_transaction', plus a scale_flag if rate x volume
    doesn't match the stated fee at normal scale but does at x100.

    IMPORTANT: this function is section-anchored. Without that, the FEES
    section's "AUTHORISATION REQUEST 250 TRANSACTIONS AT .039500" row (and
    others like it) matches PER_TXN_ROW just as well as a real Service
    Charges row does - confirmed on the real Clover_Statement_Apr_22
    statement, where an unanchored version of this function pulled in
    39 rows instead of the correct ~29, because it kept reading straight
    through into the FEES section. Only lines between a "SERVICE CHARGES"
    header and the next "Total" line (its section's own total, not one of
    the earlier all-zero sections) are considered. The header repeats
    across a page break - re-seeing it while already inside the section
    is a no-op, not a reset."""
    items = []
    stated_total = None
    in_section = False
    pending_wrap = None  # holds an incomplete PCT_ROW prefix awaiting its next line
    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        if _is_section_header(line, "SERVICE CHARGES"):
            in_section = True
            continue

        if not in_section:
            continue

        line = ROW_PREFIX.sub("", line)

        if pending_wrap is not None:
            rest = DATE_PREFIX.sub("", line)
            combined = f"{pending_wrap} {rest}"
            m = PCT_ROW.match(combined)
            pending_wrap = None
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
                    "category": _categorize(m.group("desc").strip()),
                    "scale_flag": scale_flag,
                })
                continue
            # else fall through - not a real wrap continuation, keep processing normally

        tm = TOTAL_ROW.match(line)
        if tm:
            stated_total = _f(tm.group(1))
            break  # this is the section's own closing total - stop here

        wp = PCT_ROW_WRAP_PREFIX.match(line)
        if wp and not PCT_ROW.match(line):
            pending_wrap = line
            continue

        m = PCT_ROW.match(line)
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
                "rate": rate,
                "volume": volume,
                "fee": fee,
                "category": _categorize(m.group("desc").strip()),
                "scale_flag": scale_flag,
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
                "category": _categorize(m.group("desc").strip()),
                "scale_flag": None,
            })
            continue

        # Chain-format fallback: same percentage row, but with no volume
        # figure printed at all - capture rate + fee only, volume unknown.
        m = PCT_ROW_NO_VOLUME.match(line)
        if m:
            rate = float(m.group("rate"))
            fee = _f(m.group("fee"))
            items.append({
                "description": m.group("desc").strip(),
                "row_type": "percentage_no_volume",
                "rate": rate,
                "volume": None,
                "fee": fee,
                "category": _categorize(m.group("desc").strip()),
                "scale_flag": None,
            })
            continue

    return items, stated_total


if __name__ == "__main__":
    rosemount_service_charges = """REFUND TXN CHARGE 5 TRANSACTIONS AT .500000 -2.50
VISA BUS DR CARD NQ SALE T/FEE 4 TRANSACTIONS AT .025289 -0.10
VISA BUS DR CARD NQ SRV CHG .020441 DISC RATE TIMES 720.10 -14.72
MC DEBIT NQ SERVICE CHARGE .001245 DISC RATE TIMES 1,747.72 -2.18
MC DEBIT NQ SALES TRANS FEE 17 TRANSACTIONS AT .342725 -5.83
MC DEBIT CHIP SERVICE CHARGE .001245 DISC RATE TIMES 116.80 -0.15
MC DEBIT CHIP SALES TRANS FEE 5 TRANSACTIONS AT .210899 -1.05
MC DEBIT CHIP NQ SERVICE CHRG .001245 DISC RATE TIMES 30.50 -0.04
MC DBT CHP NQ SALES TRANS 1 TRANSACTIONS AT .342725 -0.34
VISA NON-QUAL SALES TRANS FEE 19 TRANSACTIONS AT .012005 -0.23
VISA DEBIT NQ SALES TRANS FEE 57 TRANSACTIONS AT .013190 -0.75
VISA NON-QUAL SERVICE CHARGE .021687 DISC RATE TIMES 2,023.79 -43.89
VISA NQ DEBIT SERVICE CHARGE .014666 DISC RATE TIMES 8,397.81 -123.16
MASTERCARD NQ SERVICE CHARGE .021687 DISC RATE TIMES 6,737.63 -146.12
MASTERCARD CHIP NQ SRV CHRG .021687 DISC RATE TIMES 317.70 -6.89
MC PURCHASE CARD NQ SRV CHRG .032335 DISC RATE TIMES 450.70 -14.57
MASTERCARD NQ SALES TRANS FEE 55 TRANSACTIONS AT .012005 -0.66
MASTERCARD CHIP NQ SALES T/FEE 5 TRANSACTIONS AT .012005 -0.06
MC PURCHASE CARD NQ SALE T/FEE 3 TRANSACTIONS AT .012928 -0.04
VISA PRCH SRV CHG(EX BUS DR) .024813 DISC RATE TIMES 75.00 -1.86
VISA PRCH SLS T/FEE(EX BUS DR) 1 TRANSACTIONS AT .013758 -0.01
VISA SERVICE CHARGE .015696 DISC RATE TIMES 1,834.75 -28.80
VISA DEBIT SERVICE CHARGE .010702 DISC RATE TIMES 611.20 -6.54
VISA CHIP SERVICE CHARGE .015696 DISC RATE TIMES 323.30 -5.07
VISA DEBIT CHIP SERVICE CHARGE .010702 DISC RATE TIMES 1,624.75 -17.39
VISA SALES TRANS FEE 21 TRANSACTIONS AT .012005 -0.25
VISA DEBIT SALES TRANS FEE 5 TRANSACTIONS AT .013190 -0.07
VISA CHIP SALES TRANS FEE 5 TRANSACTIONS AT .012005 -0.06
VISA DR CHIP SALE TRANS FEE 28 TRANSACTIONS AT .013190 -0.37
INTL MSTRO SALES TRANS FEE 1 TRANSACTIONS AT .210899 -0.21
MASTERCARD CHIP SERVICE CHARGE .015696 DISC RATE TIMES 249.60 -3.92
MC PURCHASE CARD SERV CHRG .025894 DISC RATE TIMES 183.55 -4.75
MASTERCARD CHIP SALE TRANS FEE 3 TRANSACTIONS AT .012005 -0.04
MC PURCHASE CARD SALE T/FEE 3 TRANSACTIONS AT .012928 -0.04
Total -432.66""".splitlines()

    cavendish_service_charges = """MC DEBIT CHIP SERVICE CHARGE .006500 DISC RATE TIMES 296.50 -1.93
VISA SERVICE CHARGE .012300 DISC RATE TIMES 44.80 -0.55
VISA DEBIT SERVICE CHARGE .006500 DISC RATE TIMES 405.30 -2.63
MASTERCARD SERVICE CHARGE .012300 DISC RATE TIMES 532.00 -6.54
VISA CHIP SERVICE CHARGE .012300 DISC RATE TIMES 635.40 -7.82
MASTERCARD CHIP SERVICE CHARGE .012300 DISC RATE TIMES 1,044.00 -12.84
VISA DEBIT CHIP SERVICE CHARGE .006500 DISC RATE TIMES 1,496.80 -9.73
VISA BUS DR CARD SERV CHRG .014500 DISC RATE TIMES 676.77 -9.81
Total -51.85""".splitlines()

    for name, lines in [("Rosemount Hotel Apr 2022", rosemount_service_charges),
                         ("Cavendish French Dec 2024", cavendish_service_charges)]:
        items, stated_total = parse_service_charges(lines)
        pct_rows = [i for i in items if i["row_type"] == "percentage"]
        txn_rows = [i for i in items if i["row_type"] == "per_transaction"]
        flagged = [i for i in items if i["scale_flag"]]
        computed_total = sum(i["fee"] for i in items)
        print(f"=== {name} ===")
        print(f"  Percentage rows: {len(pct_rows)}, per-transaction rows: {len(txn_rows)}, scale-flagged: {len(flagged)}")
        print(f"  Computed total: £{computed_total:.2f}  |  Stated total: £{stated_total:.2f}  |  "
              f"{'PASS' if abs(computed_total - stated_total) < 0.01 else 'FAIL'}")
        for i in flagged:
            print(f"    FLAGGED: {i['description']} - {i['scale_flag']}")
        print()
