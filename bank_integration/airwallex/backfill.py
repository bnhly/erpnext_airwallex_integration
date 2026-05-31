"""One-shot backfill of Bank Transaction reference_number from Airwallex.

The Airwallex sync skips transactions that already exist locally
(``bank_integration/airwallex/transaction.py``'s ``transaction_exists``
guard), so changing the mapping rule alone never touches rows that were
already imported. This module re-resolves the counterparty name for
already-imported Bank Transactions in a date range and updates them in
place.

Usage from Desk:
    /api/method/bank_integration.airwallex.backfill.backfill_reference_numbers?from_date=2026-05-01&to_date=2026-06-01

Safe to re-run. Only writes a value when the resolver returns something
non-empty, so rows that legitimately have no resolvable counterparty
(ADJUSTMENT, unmatched CARD rows) stay untouched.
"""

import frappe

from bank_integration.airwallex.api.financial_transactions import FinancialTransactions
from bank_integration.airwallex.utils import (
    _resolve_party_name,
    build_expense_merchant_index,
)


def _iso_range(from_date, to_date):
    from_iso = f"{from_date}T00:00:00Z"
    to_iso = f"{to_date}T23:59:59Z"
    return from_iso, to_iso


def _fetch_financial_transactions_map(api, from_iso, to_iso):
    """Return a {transaction_id: txn_dict} map for the window. Paginates
    so larger windows still work."""
    by_id = {}
    page_num = 0
    while True:
        resp = api.get_list(
            from_created_at=from_iso,
            to_created_at=to_iso,
            page_num=page_num,
            page_size=1000,
        ) or {}
        items = resp.get("items") or resp.get("data") or []
        if not items:
            break
        for txn in items:
            tid = txn.get("id")
            if tid:
                by_id[tid] = txn
        if not resp.get("has_more"):
            break
        page_num += 1
    return by_id


@frappe.whitelist()
def backfill_reference_numbers(from_date, to_date, client_index=0, dry_run=0):
    """Backfill ``reference_number`` on Bank Transactions in the date
    range using the current resolver logic.

    Args:
        from_date / to_date: ISO date strings (YYYY-MM-DD).
        client_index: which Airwallex client row to use for credentials
            (defaults to the first one, matching the live sync).
        dry_run: pass 1 to report what would change without writing.

    Returns:
        Summary dict with counts.
    """
    settings = frappe.get_single("Bank Integration Setting")
    if not settings.airwallex_clients:
        return {"error": "no Airwallex clients configured"}
    client = settings.airwallex_clients[int(client_index)]
    api_url = settings.api_url
    from_iso, to_iso = _iso_range(from_date, to_date)
    dry_run = bool(int(dry_run))

    ft_api = FinancialTransactions(
        client_id=client.airwallex_client_id,
        api_key=client.get_password("airwallex_api_key"),
        api_url=api_url,
    )
    txn_by_id = _fetch_financial_transactions_map(ft_api, from_iso, to_iso)
    expense_index = build_expense_merchant_index(client, api_url, from_iso, to_iso)

    bank_txns = frappe.get_all(
        "Bank Transaction",
        filters={
            "date": ["between", [from_date, to_date]],
            "airwallex_source_id": ["!=", ""],
        },
        fields=["name", "transaction_id", "reference_number", "airwallex_source_type"],
    )

    totals = {
        "scanned": len(bank_txns),
        "skipped_already_set": 0,
        "skipped_no_match": 0,
        "skipped_no_value": 0,
        "updated": 0,
        "samples": [],
    }

    for bt in bank_txns:
        if bt.reference_number:
            totals["skipped_already_set"] += 1
            continue
        txn = txn_by_id.get(bt.transaction_id)
        if not txn:
            totals["skipped_no_match"] += 1
            continue
        new_ref = _resolve_party_name(txn, client, api_url, expense_index)
        if not new_ref:
            totals["skipped_no_value"] += 1
            continue
        if len(totals["samples"]) < 10:
            totals["samples"].append({
                "bank_transaction": bt.name,
                "source_type": bt.airwallex_source_type,
                "reference_number": new_ref,
            })
        if not dry_run:
            frappe.db.set_value(
                "Bank Transaction",
                bt.name,
                "reference_number",
                new_ref,
                update_modified=False,
            )
        totals["updated"] += 1

    if not dry_run:
        frappe.db.commit()
    totals["dry_run"] = dry_run
    return totals
