import frappe
from datetime import datetime

from bank_integration.airwallex.api.base_api import AirwallexAPIError
from bank_integration.airwallex.api.deposits import Deposits
from bank_integration.airwallex.api.expenses import Expenses
from bank_integration.airwallex.api.transfers import Transfers


def build_expense_merchant_index(client, api_url, from_iso, to_iso):
    """Pull every Spend Expense in the window and return a list of entries
    we can match CARD_PURCHASE / CARD_REFUND financial transactions
    against. One API list call per sync, not one per transaction.

    The Issuing Transactions API would be the canonical source of merchant
    info but it is gated on this tenant's client_id (returns 403). The
    Spend Expense for the same card swipe carries the original card
    transaction amount + currency + settled_at, which is enough to match
    by ``(abs(amount), currency)`` with ``settled_at`` as a tiebreaker.

    On this tenant the Spend ``merchant`` field arrives null on every
    row (the payment network isn't passing it through), so we fall back
    to the user-entered ``description`` (cardholder memo) which is what
    the team actually uses to identify the spend. Field used for the
    output is whichever is populated, in that order.

    Returns a list of dicts: ``{amount, currency, settled_at, merchant}``.
    Returns ``[]`` on any error or when called without credentials.
    """
    if not (client and api_url and from_iso and to_iso):
        return []
    try:
        api = Expenses(
            client_id=client.airwallex_client_id,
            api_key=client.get_password("airwallex_api_key"),
            api_url=api_url,
        )
        entries = []
        for expense in api.iter_all(from_created_at=from_iso, to_created_at=to_iso):
            merchant = (expense.get("merchant") or "").strip()
            description = (expense.get("description") or "").strip()
            name = merchant or description
            if not name:
                continue
            settled_at = expense.get("settled_at") or expense.get("created_at") or ""

            # Foreign-currency card swipes carry two amounts on the
            # expense: the original transaction value (card_transaction)
            # and what the wallet was actually charged (billing). The
            # corresponding financial_transactions row carries whichever
            # one Airwallex routes through to the ledger - usually the
            # billing amount, but indexing both is robust and cheap.
            keys = set()

            ct = expense.get("card_transaction") or {}
            try:
                ct_amount = round(abs(float(ct.get("amount"))), 2)
            except (TypeError, ValueError):
                ct_amount = 0
            ct_currency = (ct.get("currency") or "").upper()
            if ct_amount and ct_currency:
                keys.add((ct_amount, ct_currency))

            try:
                bill_amount = round(abs(float(expense.get("billing_amount"))), 2)
            except (TypeError, ValueError):
                bill_amount = 0
            bill_currency = (expense.get("billing_currency") or "").upper()
            if bill_amount and bill_currency:
                keys.add((bill_amount, bill_currency))

            for amount, currency in keys:
                entries.append({
                    "amount": amount,
                    "currency": currency,
                    "settled_at": settled_at,
                    "merchant": name,
                })
        return entries
    except AirwallexAPIError as e:
        frappe.logger().info(
            f"Expense list for merchant index failed: {e.status_code} {str(e.message)[:200]}"
        )
        return []


def _iso_to_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _match_card_merchant(txn, expense_index):
    """Find the merchant for a CARD_PURCHASE / CARD_REFUND by matching
    against the expense index built earlier in the sync. Returns the
    merchant name or empty string when there is no unambiguous match."""
    if not expense_index:
        return ""
    try:
        target_amount = round(abs(float(txn.get("amount") or txn.get("net") or 0)), 2)
    except (TypeError, ValueError):
        return ""
    target_currency = (txn.get("currency") or "").upper()
    if not (target_amount and target_currency):
        return ""

    candidates = [
        e for e in expense_index
        if e["amount"] == target_amount and e["currency"] == target_currency
    ]
    if not candidates:
        return ""
    if len(candidates) == 1:
        return candidates[0]["merchant"]

    # Multiple candidates with the same (amount, currency). If they all
    # carry the same name string, there is no ambiguity to resolve - the
    # answer is identical whichever expense we pick. This is the common
    # case for repeated identical card swipes at the same merchant
    # (e.g. two 99 AUD purchases on the same day).
    unique_names = {c["merchant"] for c in candidates}
    if len(unique_names) == 1:
        return candidates[0]["merchant"]

    target_dt = _iso_to_dt(txn.get("settled_at") or txn.get("created_at"))
    if not target_dt:
        return ""
    scored = []
    for e in candidates:
        e_dt = _iso_to_dt(e["settled_at"])
        if e_dt:
            scored.append((abs((e_dt - target_dt).total_seconds()), e["merchant"]))
    if not scored:
        return ""
    scored.sort(key=lambda pair: pair[0])
    # Require the closest match to be at least twice as close as the
    # second best, otherwise the result is ambiguous and we leave it
    # blank rather than guess.
    if len(scored) > 1 and scored[1][0] > 0 and scored[0][0] / max(scored[1][0], 1) > 0.5:
        return ""
    return scored[0][1]


def _resolve_party_name(txn, client, api_url, expense_index=None):
    """Return the counterparty name for the transaction, or empty string.

    Used to populate the Bank Transaction ``reference_number`` field so
    accountants can match the row to a Payment Entry on a counterparty
    name rather than on the (mostly null) Airwallex batch_id.

    Lookups by source_type:
      DEPOSIT                       -> GET /api/v1/deposits/{source_id}.payer_name
      TRANSFER                      -> GET /api/v1/transfers/{source_id}.beneficiary
      CARD_PURCHASE / CARD_REFUND   -> match against ``expense_index``
                                       built once per sync from Spend
                                       Expenses (see
                                       ``build_expense_merchant_index``).
    All other source_types return "".
    """
    source_type = txn.get("source_type")
    source_id = txn.get("source_id")

    if source_type in ("CARD_PURCHASE", "CARD_REFUND"):
        return _match_card_merchant(txn, expense_index)

    if not (source_id and client and api_url):
        return ""

    if source_type == "DEPOSIT":
        try:
            deposit = Deposits(
                client_id=client.airwallex_client_id,
                api_key=client.get_password("airwallex_api_key"),
                api_url=api_url,
            ).get_by_id(source_id)
        except AirwallexAPIError as e:
            frappe.logger().info(
                f"Deposit lookup failed for {source_id}: {e.status_code} {str(e.message)[:200]}"
            )
            return ""
        return (deposit.get("payer_name") or "").strip()

    if source_type == "TRANSFER":
        try:
            transfer = Transfers(
                client_id=client.airwallex_client_id,
                api_key=client.get_password("airwallex_api_key"),
                api_url=api_url,
            ).get_by_id(source_id)
        except AirwallexAPIError as e:
            frappe.logger().info(
                f"Transfer lookup failed for {source_id}: {e.status_code} {str(e.message)[:200]}"
            )
            return ""
        beneficiary = transfer.get("beneficiary") or {}
        bank_details = beneficiary.get("bank_details") or {}
        digital_wallet = beneficiary.get("digital_wallet") or {}
        first = (beneficiary.get("first_name") or "").strip()
        last = (beneficiary.get("last_name") or "").strip()
        candidates = [
            bank_details.get("account_name"),
            digital_wallet.get("account_name"),
            beneficiary.get("company_name"),
            f"{first} {last}".strip() if (first or last) else None,
        ]
        for value in candidates:
            if value and value.strip():
                return value.strip()
        return ""

    return ""


def map_airwallex_status_to_erpnext(airwallex_status):
    """
    Maps Airwallex transaction status to ERPNext Bank Transaction status.

    Args:
        airwallex_status (str): Airwallex transaction status

    Returns:
        str: ERPNext Bank Transaction status
    """
    status_mapping = {
        "PENDING": "Unreconciled",
        "SETTLED": "Settled",
        "CANCELLED": "Cancelled"
    }

    return status_mapping.get(airwallex_status.upper(), "Unreconciled")

def map_airwallex_to_erpnext(txn, bank_account, client=None, api_url=None, expense_index=None):
    """
    Maps an Airwallex transaction to ERPNext Bank Transaction format.

    Args:
        txn (dict): Airwallex transaction payload.
        bank_account (str): ERPNext Bank Account name.
        client: Airwallex Client child-table row (provides credentials for
            secondary lookups such as the Deposits and Transfers APIs).
            Optional so the test harness can build a mapping without API
            access.
        api_url (str): Base Airwallex API URL. Required together with
            ``client`` for the counterparty lookups to run.
        expense_index (list[dict]): Result of
            ``build_expense_merchant_index`` for the same sync window,
            used to populate the merchant on CARD_PURCHASE / CARD_REFUND
            rows. Optional.

    Returns:
        dict: ERPNext Bank Transaction dictionary.
    """
    # Get the amount first
    amount = txn.get("net", 0)

    # Determine transaction direction
    is_deposit = amount > 0

    # Get transaction currency
    txn_currency = txn.get("currency", "")

    # Check if bank account currency matches transaction currency
    mapped_bank_account = None
    if bank_account and txn_currency:
        try:
            # Fetch the bank account currency from the database
            account = frappe.db.get_value("Bank Account", bank_account, "account")
            bank_account_currency = frappe.db.get_value("Account", account, "account_currency")

            # Only map if currencies match
            if bank_account_currency == txn_currency:
                mapped_bank_account = bank_account
            else:
                frappe.logger().info(
                    f"Currency mismatch: Transaction {txn.get('id')} currency {txn_currency} "
                    f"doesn't match Bank Account {bank_account} currency {bank_account_currency}"
                )
        except Exception as e:
            frappe.log_error(f"Error fetching bank account currency: {str(e)}")

    return {
        "doctype": "Bank Transaction",
        "date": txn.get("created_at", "")[:10],  # YYYY-MM-DD
        "status": map_airwallex_status_to_erpnext(txn.get("status", "PENDING")),
        "bank_account": mapped_bank_account,
        "currency": txn_currency,
        "description": txn.get("description") or txn.get("source_type", ""),
        "reference_number": _resolve_party_name(txn, client, api_url, expense_index) or txn.get("batch_id", ""),
        "transaction_id": txn.get("id"),
        "transaction_type": txn.get("transaction_type", ""),
        "deposit": amount if is_deposit else 0,
        "withdrawal": abs(amount) if not is_deposit else 0,  # Use abs() for withdrawal amounts
        "airwallex_source_type": txn.get("source_type", ""),
        "airwallex_source_id": txn.get("source_id", "")
    }

def test_airwallex_mapping():
    # bench execute bank_integration.bank_integration.airwallex.utils.test_airwallex_mapping
    airwallex_txn = {
    "amount": 200.21,
    "batch_id": "bat_20201202_SGD_2",
    "client_rate": 6.93,
    "created_at": "2021-03-22T16:08:02",
    "currency": "CNY",
    "currency_pair": "AUDUSD",
    "description": "deposit to",
    "estimated_settled_at": "2021-03-22T16:08:02",
    "fee": 0,
    "funding_source_id": "99d23411-234-22dd-23po-13sd7c267b9e",
    "id": "7f687fe6-dcf4-4462-92fa-80335301d9d2",
    "net": 100.21,
    "settled_at": "2021-03-22T16:08:02",
    "source_id": "9f687fe6-dcf4-4462-92fa-80335301d9d2",
    "source_type": "PAYMENT_ATTEMPT",
    "status": "PENDING",
    "transaction_type": "PAYMENT"
    }

    erpnext_txn = map_airwallex_to_erpnext(airwallex_txn, "Your Bank Account Name")
    doc = frappe.get_doc(erpnext_txn)
    doc.insert()
    print(f"Created: {doc.name}")


def _get_airwallex_client(client_index=0):
    """Resolve credentials for an Airwallex Client row. Used by the generic
    diagnostic helpers below. Role-gated by their callers, not here."""
    settings = frappe.get_single("Bank Integration Setting")
    if not settings.airwallex_clients:
        frappe.throw("No Airwallex Clients configured on Bank Integration Setting.")
    try:
        client = settings.airwallex_clients[int(client_index)]
    except (IndexError, ValueError):
        frappe.throw(f"client_index {client_index} out of range.")
    return client, settings.api_url


@frappe.whitelist()
def airwallex_api_get(endpoint, params_json=None, client_index=0):
    """Generic read-only Airwallex GET probe. Calls any endpoint via
    AirwallexBase and returns the raw JSON. Lets us spot-check new endpoints
    on play without a code push per investigation.

    Usage:
      /api/method/bank_integration.airwallex.utils.airwallex_api_get
          ?endpoint=issuing/transactions/<id>
      /api/method/bank_integration.airwallex.utils.airwallex_api_get
          ?endpoint=spend/expenses&params_json={"page_size":5}

    Restricted to Accounts Manager because the Airwallex API key has full
    read access across the tenant and arbitrary endpoint calls can expose
    sensitive data (card numbers, beneficiary bank details, etc.).
    """
    if "Accounts Manager" not in frappe.get_roles():
        frappe.throw("Only Accounts Manager may run this probe.", frappe.PermissionError)
    if not endpoint:
        frappe.throw("endpoint is required.")

    import json as _json
    from bank_integration.airwallex.api.base_api import AirwallexBase

    params = None
    if params_json:
        try:
            params = _json.loads(params_json)
        except (TypeError, ValueError) as e:
            frappe.throw(f"params_json is not valid JSON: {e}")

    client, api_url = _get_airwallex_client(client_index)
    api = AirwallexBase(
        client_id=client.airwallex_client_id,
        api_key=client.get_password("airwallex_api_key"),
        api_url=api_url,
    )
    try:
        return api.get(endpoint=endpoint.lstrip("/"), params=params)
    except AirwallexAPIError as e:
        return {"error": True, "status_code": e.status_code, "message": str(e.message)[:500]}


@frappe.whitelist()
def probe_issuing(limit=5):
    """Issuing-API trial probe. Pairs recent CARD_PURCHASE / CARD_REFUND
    Bank Transactions with their Issuing merchant payload so the result can
    be eyeballed before we wire Issuing into _resolve_party_name.

    Remove once the merchant + description combo is shipped.
    """
    if "Accounts Manager" not in frappe.get_roles():
        frappe.throw("Only Accounts Manager may run this probe.", frappe.PermissionError)

    rows = frappe.db.sql(
        """
        SELECT name, airwallex_source_id, reference_number, description, date
        FROM `tabBank Transaction`
        WHERE airwallex_source_type IN ('CARD_PURCHASE', 'CARD_REFUND')
          AND airwallex_source_id IS NOT NULL
          AND airwallex_source_id != ''
        ORDER BY date DESC
        LIMIT %s
        """,
        (int(limit),),
        as_dict=True,
    )

    out = []
    for r in rows:
        entry = {
            "bt": r.name,
            "current_ref": r.reference_number,
            "description": r.description,
            "source_id": r.airwallex_source_id,
        }
        txn = airwallex_api_get(f"issuing/transactions/{r.airwallex_source_id}")
        if isinstance(txn, dict) and txn.get("error"):
            entry["error"] = f"{txn.get('status_code')} {txn.get('message')}"
        else:
            entry["issuing_merchant"] = (txn or {}).get("merchant") or {}
            entry["issuing_top_level_keys"] = sorted((txn or {}).keys())
        out.append(entry)

    return out

