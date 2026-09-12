"""
Global Payments (GPUK LLP) card product code -> category mapping.

Confidence levels:
  CONFIRMED  - matches GP's own statement wording AND/OR corroborated by the
               takepayments legend with no conflict
  LIKELY     - inferred from naming convention only, no conflicting evidence
  DISPUTED   - takepayments legend gives a DIFFERENT answer than GP's own
               statement wording implied. Flagged for validation against
               real transaction volume/mix, not blindly trusted either way.

Categories: debit, credit, business_debit, business_credit, cnp,
            pci, amex, international, unmapped
"""

CATEGORY_MAP = {
    # code: (category, confidence, note)
    "VDCD":  ("business_debit", "DISPUTED",
              "GP wording says 'Consumer Domestic' (-> debit); takepayments "
              "legend says 'Visa Commercial Debit' (-> business_debit). "
              "Validate against transaction mix before trusting."),
    "VICP":  ("credit", "LIKELY", "GP: generic credit tier"),
    "VIPU":  ("business_credit", "CONFIRMED", "Visa Purchasing - both sources agree"),
    "VISA":  ("credit", "CONFIRMED", "generic/base Visa credit"),
    "VIBS":  ("business_credit", "CONFIRMED", "Visa Business - both sources agree"),
    "VDBT":  ("debit", "CONFIRMED", "Visa Debit - both sources agree"),
    "VINF":  ("credit", "LIKELY", "Visa Infinite (Premium consumer, not business)"),
    "VIPL":  ("credit", "LIKELY", "Visa Platinum (Premium consumer, not business)"),
    "VIGD":  ("credit", "LIKELY", "Visa Gold (Premium consumer, not business)"),

    "MCPP":  ("credit", "DISPUTED",
              "GP wording implied 'Purchasing' (-> business); takepayments "
              "legend says 'MasterCard Prepaid Consumer' (-> plain credit). "
              "Validate against transaction mix."),
    "MDCD":  ("business_debit", "DISPUTED",
              "GP wording says 'Debit Consumer Domestic' (-> debit); "
              "takepayments legend says 'MasterCard Commercial Debit' "
              "(-> business_debit). Validate against transaction mix."),
    "MCCP":  ("business_credit", "CONFIRMED", "MasterCard Corporate - both sources agree"),
    "MDPD":  ("debit", "CONFIRMED", "MasterCard Premium Debit - both sources agree"),
    "MCFL":  ("business_credit", "LIKELY", "MasterCard Fleet, per legend"),
    "MCGD":  ("credit", "LIKELY", "MasterCard Gold, per legend - plain consumer"),
    "MCPC":  ("business_credit", "DISPUTED",
              "GP wording unclear ('debit-adjacent' guess); takepayments "
              "legend says 'MasterCard PrePaid Commercial' (-> business). "
              "Validate against transaction mix."),
    "MCNW":  ("credit", "LIKELY", "MasterCard New World, per legend"),
    "MCPL":  ("credit", "LIKELY", "MasterCard Platinum, per legend"),
    "MCWS":  ("credit", "LIKELY", "MasterCard World Signia, per legend"),
    "MC":    ("credit", "CONFIRMED", "generic MasterCard credit"),
    "MCBS":  ("business_credit", "CONFIRMED", "MasterCard Business Card - both sources agree"),
    "MCWC":  ("credit", "LIKELY", "MasterCard World Card, per legend (plain consumer)"),

    # CNP-related lines in the Transactions Charges table
    "VISA 3DS AUTHENTICATION FEE":  ("cnp", "CONFIRMED", "3DS = online/CNP authentication"),
    "MCARD 3DS2 AUTHENTICATION FEE": ("cnp", "CONFIRMED", "3DS2 = online/CNP authentication"),
    "NON SECURE FEE": ("cnp", "CONFIRMED", "explicit non-secure/CNP signal"),
}

# "Merchandise Rtn" suffix = refund on that same card type, not a new category
REFUND_SUFFIX = "Merchandise Rtn"


def categorize(code: str):
    """Look up a product code, stripping a refund suffix if present."""
    is_refund = REFUND_SUFFIX in code
    base_code = code.replace(REFUND_SUFFIX, "").strip()
    entry = CATEGORY_MAP.get(base_code)
    if entry is None:
        return {"category": "unmapped", "confidence": "NONE",
                "note": f"'{base_code}' not in mapping table", "is_refund": is_refund}
    category, confidence, note = entry
    return {"category": category, "confidence": confidence, "note": note, "is_refund": is_refund}
