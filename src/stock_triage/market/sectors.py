"""IDX-IC sectors: index codes and the sector names company records use."""

from __future__ import annotations

# Sector index code -> the IDX-IC sector name in ``companies.sector``.
SECTOR_NAMES = {
    "IDXENERGY": "Energi",
    "IDXBASIC": "Barang Baku",
    "IDXINDUST": "Perindustrian",
    "IDXNONCYC": "Barang Konsumen Primer",
    "IDXCYCLIC": "Barang Konsumen Non-Primer",
    "IDXHEALTH": "Kesehatan",
    "IDXFINANCE": "Keuangan",
    "IDXPROPERT": "Properti & Real Estat",
    "IDXTECHNO": "Teknologi",
    "IDXINFRA": "Infrastruktur",
    "IDXTRANS": "Transportasi & Logistik",
}
SECTOR_LABELS = {
    "IDXENERGY": "Energy",
    "IDXBASIC": "Basic materials",
    "IDXINDUST": "Industrials",
    "IDXNONCYC": "Consumer staples",
    "IDXCYCLIC": "Consumer cyclicals",
    "IDXHEALTH": "Healthcare",
    "IDXFINANCE": "Financials",
    "IDXPROPERT": "Property",
    "IDXTECHNO": "Technology",
    "IDXINFRA": "Infrastructure",
    "IDXTRANS": "Transport and logistics",
}
_BY_NAME = {name.lower(): code for code, name in SECTOR_NAMES.items()}


def sector_code(name: str | None) -> str | None:
    """The index code for a company's sector name, or None when it is not an IDX-IC sector."""
    if not name:
        return None
    text = name.strip()
    if text.upper() in SECTOR_NAMES:
        return text.upper()
    return _BY_NAME.get(text.lower())
