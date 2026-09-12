"""
Elavon (U.S. Bank Europe DAC) statement parser.
Validated against real statements: Pattisons and Palomino Ltd (Aug 2026),
Statement 00300-9212095088 (Jul 2023) - via a live run of pdftotext
-layout against the actual PDFs, not hand-typed test lines.
"""
import re

CARD_FEES_MAP = {
    "VISA CONSUMER CR": ("credit", "CONFIRMED"),
    "VISA CONSUMER CR NON SEC": ("credit", "CONFIRMED"),   # + CNP signal, see note below
    "VISA NON-EEA": ("international", "CONFIRMED"),
    "VISA BUSINESS CR": ("business_credit", "CONFIRMED"),
    "VISA PURCHASING": ("business_credit", "CONFIRMED"),
    "VISA CORPORATE": ("business_credit", "CONFIRMED"),
    "VISA CONSUMER DB": ("debit", "CONFIRMED"),
    "VISA CONSUMER DB NON SEC": ("debit", "CONFIRMED"),
    "VISA BUSINESS DB": ("business_debit", "CONFIRMED"),
    "VISA BUSINESS DB NON SEC": ("business_debit", "CONFIRMED"),
    "MCARD/MAESTRO NON-EEA": ("international", "CONFIRMED"),
    "M/CARD CONSUMER CR": ("credit", "CONFIRMED"),
    "M/CARD CONSUMER CR NON SEC": ("credit", "CONFIRMED"),
    "M/CARD PURCHASING": ("business_credit", "CONFIRMED"),
    "M/CARD CORPORATE": ("business_credit", "CONFIRMED"),
    "M/CARD CONSUMER DEBIT": ("debit", "CONFIRMED"),
    "M/CARD CONSUMER DB NON SEC": ("debit", "CONFIRMED"),
    "M/CARD BUSINESS": ("business_debit", "CONFIRMED"),
    "M/CARD PPAID COMMERCIAL": ("business_debit", "CONFIRMED"),
    "AMERICAN EXPRESS": ("amex", "CONFIRMED"),
}
# "NON SEC" suffix = non-secure/CNP-flagged, per Trendy Togs statement.
# It's an ADDITIONAL signal on top of the base category, not a category swap -
# flagged separately so a caller can also bucket these into a CNP view.
CNP_SUFFIX = "NON SEC"

OTHER_FEES_MAP = {
    "SECURED PCI": ("pci", "CONFIRMED"),
}

CARD_FEES_ROW = re.compile(
    r"^(?P<desc>.+?)\s+"
    r"(?P<volume>[\d,]+\.\d{2})\s+"
    r"(?P<items>[\d,]+)\s+"
    r"(?P<disc_rate>[\d.]+)\s+"
    r"(?P<per_item_rate>[\d.]+)\s+"
    r"(?P<fee>[\d,]+\.\d{2})\s*$"
)
CARD_FEES_TOTAL = re.compile(r"^Total\s+([\d,]+\.\d{2})\s*$")


def _f(s):
    return float(s.replace(",", ""))


def parse_card_fees(lines):
    """lines: full statement text, exactly as pdftotext -layout extracts
    it. Section-anchored on the "Card Fees" header line: without this,
    the statement's earlier summary block contains a standalone "Total"
    line for the whole bill (e.g. "Total  473.59" under the Useful
    Information table) that happens to match CARD_FEES_TOTAL before the
    real per-card rows are ever reached - confirmed on the real Pattisons
    statement, where an unanchored version of this function returned 0
    rows because it hit that summary Total first and stopped."""
    items = []
    stated_total = None
    in_section = False
    for line in lines:
        line = line.strip()
        if not line:
            continue

        if line == "Card Fees":
            in_section = True
            continue

        if not in_section:
            continue

        tm = CARD_FEES_TOTAL.match(line)
        if tm:
            stated_total = _f(tm.group(1))
            break
        m = CARD_FEES_ROW.match(line)
        if not m:
            continue
        desc = m.group("desc").strip()
        # One real statement used a Unicode soft hyphen (U+00AD) instead of
        # a plain ASCII "-" in "VISA NON-EEA" / "MCARD/MAESTRO NON-EEA" -
        # normalise before lookup so these aren't wrongly left "unmapped".
        desc_normalized = desc.replace("\u00ad", "-")
        base_desc = desc_normalized.replace(" " + CNP_SUFFIX, "").strip()
        is_cnp = CNP_SUFFIX in desc_normalized
        entry = CARD_FEES_MAP.get(desc_normalized) or CARD_FEES_MAP.get(base_desc)
        category, confidence = entry if entry else ("unmapped", "NONE")
        items.append({
            "description": desc,
            "volume": _f(m.group("volume")),
            "items": int(m.group("items").replace(",", "")),
            "discount_rate": float(m.group("disc_rate")),
            "per_item_rate": float(m.group("per_item_rate")),
            "fee": _f(m.group("fee")),
            "category": category,
            "confidence": confidence,
            "is_cnp": is_cnp,
        })
    return items, stated_total


if __name__ == "__main__":
    # Three real statements, Card Fees table only (the core rate-bearing table)
    statements = {
        "Pattisons Aug 2026": {
            "lines": """VISA CONSUMER CR 3,233.65 251 0.6900 0.0200 27.33
VISA NON-EEA 416.18 21 3.3500 0.0200 14.36
VISA BUSINESS CR 118.75 8 1.8500 0.0200 2.36
VISA PURCHASING 7.90 1 1.8500 0.0200 0.17
VISA CORPORATE 234.18 6 1.8500 0.0200 4.45
VISA CONSUMER DB 23,122.05 2,171 0.3500 0.0200 124.34
VISA BUSINESS DB 708.54 41 1.0800 0.0200 8.47
MCARD/MAESTRO NON-EEA 60.84 6 3.3500 0.0200 2.16
M/CARD CONSUMER CR 4,433.33 364 0.6900 0.0200 37.87
M/CARD PURCHASING 44.30 2 1.8500 0.0200 0.86
M/CARD CORPORATE 688.09 61 1.8500 0.0200 13.94
M/CARD CONSUMER DEBIT 16,338.17 1,628 0.3500 0.0200 89.75
M/CARD BUSINESS 36.73 6 1.8500 0.0200 0.80
M/CARD PPAID COMMERCIAL 357.70 6 1.8500 0.0200 6.73
AMERICAN EXPRESS 2,182.71 121 1.9000 0.0000 41.47
Total 375.06""".splitlines(),
            "stated": 375.06,
        },
        "Pattisons Jul 2026": {
            "lines": """VISA CONSUMER CR 3,151.89 283 0.6900 0.0200 27.41
VISA BUSINESS CR 216.79 11 1.8500 0.0200 4.23
VISA PURCHASING 23.50 1 1.8500 0.0200 0.45
VISA CORPORATE 38.55 4 1.8500 0.0200 0.79
VISA NON-EEA 95.08 8 3.3500 0.0200 3.34
VISA CONSUMER DB 24,615.60 2,376 0.3500 0.0200 133.68
VISA BUSINESS DB 769.23 54 1.0800 0.0200 9.39
MCARD/MAESTRO NON-EEA 147.65 9 3.3500 0.0200 5.13
M/CARD CONSUMER CR 3,611.85 330 0.6900 0.0200 31.52
M/CARD CORPORATE 720.40 74 1.8500 0.0200 14.82
M/CARD CONSUMER DEBIT 15,794.56 1,676 0.3500 0.0200 88.81
M/CARD BUSINESS 79.86 9 1.8500 0.0200 1.66
M/CARD PPAID COMMERCIAL 472.17 11 1.8500 0.0200 8.96
AMERICAN EXPRESS 1,576.95 126 1.9000 0.0000 29.96
Total 360.15""".splitlines(),
            "stated": 360.15,
        },
        "Trendy Togs Jul 2023": {
            "lines": """VISA CONSUMER CR 5,107.56 81 0.6500 0.0000 33.20
VISA CONSUMER CR NON SEC 101.96 3 0.9000 0.0000 0.92
VISA NON-EEA 485.75 11 3.1650 0.0000 15.36
VISA CORPORATE 183.44 1 1.8500 0.0000 3.39
VISA CONSUMER DB 24,494.65 478 0.2750 0.0000 67.36
VISA CONSUMER DB NON SEC 2,047.15 51 0.4100 0.0000 8.39
VISA BUSINESS DB 124.46 5 1.5000 0.0000 1.87
VISA BUSINESS DB NON SEC 19.99 1 1.8900 0.0000 0.38
MCARD/MAESTRO NON-EEA 264.87 4 3.1650 0.0000 8.39
M/CARD CONSUMER CR 16,732.35 261 0.6500 0.0000 108.77
M/CARD CONSUMER CR NON SEC 1,540.82 29 0.9000 0.0000 13.87
M/CARD PURCHASING 51.98 1 1.8500 0.0000 0.96
M/CARD CONSUMER DEBIT 19,221.39 324 0.3000 0.0000 57.65
M/CARD CONSUMER DB NON SEC 1,561.79 32 0.4100 0.0000 6.40
M/CARD BUSINESS 34.99 1 1.8500 0.0000 0.65
M/CARD CORPORATE 271.88 4 1.8500 0.0000 5.02
M/CARD PPAID COMMERCIAL 124.96 1 1.8500 0.0000 2.31
Total 334.89""".splitlines(),
            "stated": 334.89,
        },
    }

    for name, data in statements.items():
        items, stated_total = parse_card_fees(data["lines"])
        computed = sum(i["fee"] for i in items)
        unmapped = [i for i in items if i["category"] == "unmapped"]
        cnp_count = sum(1 for i in items if i["is_cnp"])
        print(f"=== {name} ===")
        print(f"  Rows parsed: {len(items)}, unmapped: {len(unmapped)}, CNP-flagged rows: {cnp_count}")
        print(f"  Computed total: £{computed:.2f}  |  Stated total: £{stated_total:.2f}  |  "
              f"{'PASS' if abs(computed - stated_total) < 0.01 else 'FAIL'}")
        print()
