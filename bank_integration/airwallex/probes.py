"""Temporary live-API probes for the reference_number mapping work.

These are read-only helpers used once, from the Frappe Desk or via
``/api/method/...``, to capture the shape of Airwallex responses for the
specific tenant before the reference_number mapping is finalised.

Delete this file once the constants in ``utils.py`` are locked in.
"""

import frappe

from bank_integration.airwallex.api.base_api import AirwallexBase
from bank_integration.airwallex.api.financial_transactions import FinancialTransactions


def _client_credentials():
    settings = frappe.get_single("Bank Integration Setting")
    if not settings.airwallex_clients:
        frappe.throw("No Airwallex clients configured")
    client = settings.airwallex_clients[0]
    return {
        "client_id": client.airwallex_client_id,
        "api_key": client.get_password("airwallex_api_key"),
        "api_url": settings.api_url,
    }


@frappe.whitelist()
def probe_source_types(page_size=200):
    """Return one example transaction per distinct ``source_type`` seen
    in the most recent page of financial transactions."""
    api = FinancialTransactions(**_client_credentials())
    resp = api.get_list(page_num=0, page_size=int(page_size))
    items = resp.get("items") or resp.get("data") or []
    seen = {}
    for txn in items:
        source_type = txn.get("source_type")
        if source_type and source_type not in seen:
            seen[source_type] = {
                "source_id": txn.get("source_id"),
                "transaction_type": txn.get("transaction_type"),
                "batch_id": txn.get("batch_id"),
                "status": txn.get("status"),
            }
    return {"distinct_source_types": seen, "total_in_page": len(items)}


class _Transfers(AirwallexBase):
    def get_by_id(self, transfer_id):
        return self.get(endpoint=f"transfers/{transfer_id}")


class _IssuingTransactions(AirwallexBase):
    def get_by_id(self, transaction_id):
        return self.get(endpoint=f"issuing/transactions/{transaction_id}")


class _Deposits(AirwallexBase):
    def get_by_id(self, deposit_id):
        return self.get(endpoint=f"deposits/{deposit_id}")


@frappe.whitelist()
def probe_transfer(source_id):
    """Return the raw ``GET /api/v1/transfers/{id}`` response so we can
    confirm the exact beneficiary field path on the live tenant."""
    api = _Transfers(**_client_credentials())
    return api.get_by_id(source_id)


@frappe.whitelist()
def probe_issuing_transaction(source_id):
    """Return the raw ``GET /api/v1/issuing/transactions/{id}`` response so we
    can confirm the exact merchant-name field path on the live tenant."""
    api = _IssuingTransactions(**_client_credentials())
    return api.get_by_id(source_id)


@frappe.whitelist()
def probe_deposit(source_id):
    """Return the raw ``GET /api/v1/deposits/{id}`` response so we can confirm
    the exact payer-name field path on the live tenant."""
    api = _Deposits(**_client_credentials())
    return api.get_by_id(source_id)


@frappe.whitelist()
def probe_sample_per_source_type(page_size=200):
    """Return one full financial-transaction object per distinct source_type
    seen in the most recent page, so we can see what fields (especially
    ``description``) are already populated without any secondary lookup."""
    api = FinancialTransactions(**_client_credentials())
    resp = api.get_list(page_num=0, page_size=int(page_size))
    items = resp.get("items") or resp.get("data") or []
    samples = {}
    for txn in items:
        source_type = txn.get("source_type")
        if source_type and source_type not in samples:
            samples[source_type] = txn
    return samples
