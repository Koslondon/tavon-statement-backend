"""
InterCard AG (powered by Verifone) parser.
Validated against real statement: Acantha Crystals Limited, invoice
001-2428394, billing period Mar 2023.

Cleanest format of the batch: single page, proper columnar invoice layout,
no multi-line record reassembly needed at all. Every row carries a fixed
short abbreviation code (e.g. MCMSF, VDTRX) on the same line as its numbers,
so - unlike AIB or Clover's free-text descriptions - the code itself is a
reliable, unique key. Category mapping uses that code directly rather than
fuzzy-matching description text.

Two row shapes:
  1. Percentage: "[desc] [CODE] N.NNNN % [value] [price] [vat]%"
     -> price = rate% x value
  2. Per-transaction count: "[desc] [CODE] N [value=0.0000] [price] [vat]%"
     -> value/price are 0.00 on this statement (transaction fee negotiated
        to nil), but the row still carries a real transaction count that
        matters for volume/count reconciliation even when price is zero.

Description text sometimes wraps onto a following line (e.g. "Mark-up
Commercial Cards" / "Mastercard") - safe to ignore since the abbreviation
code, not the description, is the canonical identifier here.
"""
import re

CODE_INFO = {
    "KSCHN": ("End-of-day clearing transaction fee", "general"),
    "ACCMC": ("Mark-up Commercial Cards Mastercard", "business_commercial"),
    "MCMSF": ("Mastercard Credit merchant fee", "credit"),
    "MCTRX": ("Mastercard Credit transaction fee", "credit"),
    "MDMSF": ("Mastercard Debit service fee", "debit"),
    "MDTRX": ("Mastercard Debit transaction fee", "debit"),
    "ACCVI": ("Mark-up Commercial Cards Visa", "business_commercial"),
    "VCMSF": ("Visa Credit service fee", "credit"),
    "VCTRX": ("Visa Credit transaction fee", "credit"),
    "VDMSF": ("Visa Debit service fee", "debit"),
    "VDTRX": ("Visa Debit transaction fee", "debit"),
}

PCT_ROW = re.compile(
    r"^(?P<desc>.+?)\s{2,}(?P<code>[A-Z]{4,6})\s{2,}"
    r"(?P<rate>[\d.]+)\s*%\s+"
    r"(?P<value>[\d,]+\.\d{4})\s+(?P<price>-?[\d,]+\.\d{2})\s+(?P<vat>\d+)%\s*$"
)
COUNT_ROW = re.compile(
    r"^(?P<desc>.+?)\s{2,}(?P<code>[A-Z]{4,6})\s{2,}"
    r"(?P<count>\d+)\s+"
    r"(?P<value>[\d,]+\.\d{4})\s+(?P<price>-?[\d,]+\.\d{2})\s+(?P<vat>\d+)%\s*$"
)
NET_AMOUNT_ROW = re.compile(r"^\s*Net Amount\s+\d+%\s+(?P<total>[\d,]+\.\d{2})\s*$")


def _f(s):
    return float(s.replace(",", ""))


def parse_intercard_statement(lines):
    items = []
    stated_net_total = None
    for raw in lines:
        line = raw.rstrip("\n")
        if not line.strip():
            continue

        nm = NET_AMOUNT_ROW.match(line)
        if nm:
            stated_net_total = _f(nm.group("total"))
            continue

        m = PCT_ROW.match(line)
        if m:
            code = m.group("code")
            desc, category = CODE_INFO.get(code, (m.group("desc").strip(), "unmapped"))
            rate = float(m.group("rate")) / 100  # printed as "1.3000 %" -> 0.013
            value = _f(m.group("value"))
            price = _f(m.group("price"))
            computed = rate * value
            items.append({
                "code": code,
                "description": desc,
                "category": category,
                "row_type": "percentage",
                "rate_pct": float(m.group("rate")),
                "value": value,
                "price": price,
                "check_ok": abs(computed - price) < 0.02,
            })
            continue

        m = COUNT_ROW.match(line)
        if m:
            code = m.group("code")
            desc, category = CODE_INFO.get(code, (m.group("desc").strip(), "unmapped"))
            items.append({
                "code": code,
                "description": desc,
                "category": category,
                "row_type": "per_transaction_count",
                "count": int(m.group("count")),
                "value": _f(m.group("value")),
                "price": _f(m.group("price")),
            })
            continue

    return items, stated_net_total


if __name__ == "__main__":
    lines = """End-of-day clearing transaction                    KSCHN                                           29                                0.0000    0.00     0%
Mark-up Commercial Cards                           ACCMC                                       1.3000 %                              8.0000    0.10     0%
Mastercard Credit merchant fee                     MCMSF                                       1.0000 %                        423.0000        4.23     0%
Mastercard Credit transaction                      MCTRX                                           11                                0.0000    0.00     0%
Mastercard Debit service fee                       MDMSF                                       1.0000 %                        837.5000        8.38     0%
Mastercard Debit transaction                       MDTRX                                           55                                0.0000    0.00     0%
Mark-up Commercial Cards                           ACCVI                                       1.3000 %                             87.0000    1.13     0%
Visa Credit service fee                            VCMSF                                       1.0000 %                        627.0000        6.27     0%
Visa Credit transaction fee                        VCTRX                                           16                                0.0000    0.00     0%
Visa Debit service fee                             VDMSF                                       1.0000 %                     2,041.1000        20.41     0%
Visa Debit transaction fee                         VDTRX                                           90                                0.0000    0.00     0%

                                                                                                             Net Amount                 0%    40.52""".splitlines()

    items, stated_total = parse_intercard_statement(lines)
    print(f"Parsed {len(items)} rows\n")
    for i in items:
        extra = f"rate={i['rate_pct']}%" if i["row_type"] == "percentage" else f"count={i['count']}"
        check = f"  {'OK' if i.get('check_ok', True) else 'MISMATCH'}" if i["row_type"] == "percentage" else ""
        print(f"  {i['code']:6s} {i['category']:20s} {extra:14s} value={i['value']:>10.4f}  price=£{i['price']:>6.2f}{check}")

    computed_total = sum(i["price"] for i in items)
    print(f"\nComputed total: £{computed_total:.2f}  |  Stated Net Amount: £{stated_total:.2f}  |  "
          f"{'PASS' if abs(computed_total - stated_total) < 0.01 else 'FAIL'}")
