"""
Airwallex Spend Expenses -> ERPNext Purchase Invoice (DRAFT) importer.

Behaviour
- Pulls APPROVED expenses only, over a created_at date window.
- Creates one Purchase Invoice per expense, left in DRAFT (not submitted), so
  the GL account can be corrected manually before submission.
- GST is applied only to AUD expenses. For AUD the item rate is the net amount
  (billing amount / 1.1) and the GST 10% template adds the tax back to the
  billing amount. Non-AUD expenses get no tax line.
- Idempotency is entirely local. A unique field custom_tm_airwallex_expense_id
  on Purchase Invoice plus an existence check prevents duplicates. The Airwallex
  sync_status field is never read or written, so this cannot conflict with Xero.
- Triggered manually from the Bank Integration Setting form button.

Card name gap
- The expenses endpoint returns card_id (a UUID) but not the cardholder name.
  Populate CARD_NAME_MAP below (card_id -> name) to fill the card field in the
  comment. Unmapped cards leave the card blank. The PI is valid either way.

Edit the CONFIG block to match your chart of accounts if anything changes.
"""

import frappe
import requests
from frappe.utils import getdate, add_days, today, format_date
from bank_integration.airwallex.api.expenses import Expenses
from bank_integration.airwallex.api.base_api import AirwallexAPIError
from bank_integration.bank_integration.doctype.bank_integration_log import (
    bank_integration_log as bi_log,
)

# ----------------------------------------------------------------------------
# CONFIG  (values taken from the existing manually imported Purchase Invoices)
# ----------------------------------------------------------------------------
COMPANY = "Thrust Maritime Pty. Ltd."
SUPPLIER = "Airwallex Expense"
ITEM_CODE = "Airwallex generic"
DEFAULT_EXPENSE_ACCOUNT = "5135 - Cost of Goods Sold - TM"   # corrected manually after import
COST_CENTER = "Main - TM"
WAREHOUSE = "Pakenham - TM"                                   # ignored for non stock items
AUD_TAX_TEMPLATE = "GST 10% (TM)"
LETTER_HEAD = "General TM"
TITLE_PREFIX = "AirW "
GST_DIVISOR = 1.1
APPROVED_STATUS = ["APPROVED"]
IMPORT_ATTACHMENTS = True

# card_id (UUID) -> friendly cardholder name. Optional. Unmapped = blank card.
#
# Production data lives in site_config.json under the key
# ``airwallex_card_names`` so PII does not land in this public repo:
#
#     "airwallex_card_names": {
#         "<card_uuid>": "Cardholder Name",
#         ...
#     }
#
# The site config entries are merged on top of CARD_NAME_MAP at lookup
# time (via ``_card_name_map``), so anything you do hardcode here still
# works but the canonical place is site_config.json on each site.
CARD_NAME_MAP = {
    # "4497a3ce-c610-4630-870a-cbc077766518": "Ben Healy",
}


def _card_name_map():
    """Return the merged card_id -> cardholder name lookup.

    Hardcoded ``CARD_NAME_MAP`` is the base; entries under the
    ``airwallex_card_names`` key in site_config.json override and extend
    it. site_config.json is per-site and not in the repo, which is where
    PII (names / emails / card UUIDs) belongs.
    """
    overrides = frappe.get_conf().get("airwallex_card_names") or {}
    return {**CARD_NAME_MAP, **overrides}


# ----------------------------------------------------------------------------
# Entry points
# ----------------------------------------------------------------------------
@frappe.whitelist()
def enqueue_expense_import(from_date=None, to_date=None):
    """Called by the Purchase Invoice list button. Runs in the background.

    Restricted to users with the Accounts Manager role. The client side
    also hides the button for other roles, but enforcing here guards
    against direct /api/method/ calls.
    """
    if "Accounts Manager" not in frappe.get_roles():
        frappe.throw(
            "Only users with the Accounts Manager role can import Airwallex expenses.",
            frappe.PermissionError,
        )
    frappe.enqueue(
        "bank_integration.airwallex.expense_invoice.run_expense_import",
        queue="long",
        timeout=2000,
        from_date=from_date,
        to_date=to_date,
    )
    return {"status": "queued"}


def run_expense_import(from_date=None, to_date=None):
    """Import APPROVED expenses created within the window into draft PIs."""
    settings = frappe.get_single("Bank Integration Setting")

    if not getattr(settings, "airwallex_clients", None):
        bi_log.create_log("Expense import: no Airwallex clients configured", status="Error")
        return

    from_iso, to_iso = _iso_range(settings, from_date, to_date)

    totals = {"scanned": 0, "created": 0, "duplicate": 0, "errors": 0, "attachments": 0}

    for client in settings.airwallex_clients:
        try:
            api = Expenses(
                client_id=client.airwallex_client_id,
                api_key=client.get_password("airwallex_api_key"),
                api_url=settings.api_url,
            )
            token = api.get_valid_token()

            for expense in api.iter_all(
                status=APPROVED_STATUS,
                from_created_at=from_iso,
                to_created_at=to_iso,
            ):
                totals["scanned"] += 1
                expense_id = expense.get("id")

                if not expense_id:
                    continue

                if purchase_invoice_exists(expense_id):
                    totals["duplicate"] += 1
                    continue

                try:
                    pi = create_draft_invoice(expense)
                    totals["created"] += 1

                    if IMPORT_ATTACHMENTS:
                        totals["attachments"] += attach_receipts(
                            pi.name, expense, token, settings.api_url
                        )

                except Exception as e:
                    totals["errors"] += 1
                    frappe.log_error(
                        message=f"Expense {expense_id}: {str(e)[:400]}\n{frappe.get_traceback()}",
                        title=f"AirW Expense PI Error {str(expense_id)[:8]}",
                    )

        except AirwallexAPIError as e:
            totals["errors"] += 1
            frappe.log_error(
                message=f"Airwallex API error: {str(e.message)[:400]}",
                title="AirW Expense Import API Error",
            )
        except Exception as e:
            totals["errors"] += 1
            frappe.log_error(
                message=f"Expense import failed: {str(e)[:400]}\n{frappe.get_traceback()}",
                title="AirW Expense Import Error",
            )

    summary = (
        f"Airwallex expense import complete. "
        f"Scanned {totals['scanned']}, created {totals['created']} draft PIs, "
        f"skipped {totals['duplicate']} already imported, "
        f"errors {totals['errors']}, attachments {totals['attachments']}."
    )
    bi_log.create_log(summary, status="Error" if totals["errors"] else "Success")
    frappe.db.commit()

    frappe.publish_realtime(
        "expense_import_complete",
        {"status": "error" if totals["errors"] else "success", "message": summary},
    )
    return summary


# ----------------------------------------------------------------------------
# Invoice construction
# ----------------------------------------------------------------------------
def create_draft_invoice(expense):
    """Build and insert one draft Purchase Invoice for an expense."""
    expense_id = expense.get("id")
    merchant = (expense.get("merchant") or "").strip()
    memo = (expense.get("description") or merchant or "Airwallex expense").strip()
    card_name = _card_name_map().get(expense.get("card_id"), "")

    billing_currency = (expense.get("billing_currency") or "AUD").upper()
    gross = _to_float(expense.get("billing_amount"))

    card_txn = expense.get("card_transaction") or {}
    txn_currency = (card_txn.get("currency") or billing_currency).upper()
    txn_amount = _to_float(card_txn.get("amount"), default=gross)

    posting_date = (expense.get("settled_at") or expense.get("created_at") or today())[:10]

    is_aud = billing_currency == "AUD"
    net = round(gross / GST_DIVISOR, 2) if is_aud else round(gross, 2)

    company_currency = frappe.get_cached_value("Company", COMPANY, "default_currency")

    pi = frappe.new_doc("Purchase Invoice")
    pi.company = COMPANY
    pi.supplier = SUPPLIER
    pi.set_posting_time = 1
    pi.posting_date = posting_date
    pi.bill_no = memo[:140]
    pi.bill_date = posting_date
    pi.currency = billing_currency
    pi.title = (TITLE_PREFIX + (merchant or memo))[:140]
    pi.remarks = _build_comment(memo, merchant, card_name, billing_currency, gross,
                                txn_currency, txn_amount, posting_date)
    pi.update_stock = 0
    pi.disable_rounded_total = 1
    pi.letter_head = LETTER_HEAD
    pi.set("custom_tm_airwallex_expense_id", expense_id)
    # Existing field on Purchase Invoice in your system. Harmless if absent.
    if pi.meta.has_field("custom_tm_airwallex_card"):
        pi.set("custom_tm_airwallex_card", card_name)

    pi.append("items", {
        "item_code": ITEM_CODE,
        "item_name": ITEM_CODE,
        "description": memo,
        "qty": 1,
        "uom": "Unit",
        "conversion_factor": 1,
        "rate": net,
    })

    pi.set_missing_values()

    # Re-assert the defaults we want, in case supplier or item defaults changed them.
    for item in pi.items:
        item.expense_account = DEFAULT_EXPENSE_ACCOUNT
        item.cost_center = COST_CENTER
        if WAREHOUSE:
            item.warehouse = WAREHOUSE

    # Exchange rate
    if billing_currency == company_currency:
        pi.conversion_rate = 1
    else:
        try:
            from erpnext.setup.utils import get_exchange_rate
            pi.conversion_rate = get_exchange_rate(billing_currency, company_currency, posting_date) or 1
        except Exception:
            pi.conversion_rate = 1

    # Taxes: GST only for AUD. Clear anything set by defaults first.
    pi.set("taxes", [])
    if is_aud:
        from erpnext.controllers.accounts_controller import get_taxes_and_charges
        pi.taxes_and_charges = AUD_TAX_TEMPLATE
        for row in get_taxes_and_charges("Purchase Taxes and Charges Template", AUD_TAX_TEMPLATE):
            pi.append("taxes", row)

    pi.calculate_taxes_and_totals()

    _insert_with_billno_fallback(pi, expense_id)
    return pi


def _insert_with_billno_fallback(pi, expense_id):
    """Insert as draft. If the supplier invoice number uniqueness check blocks a
    repeated memo, retry once using the expense id as bill_no so the batch does
    not die. The memo stays in the item description and remarks."""
    try:
        pi.insert(ignore_permissions=True)
        return
    except frappe.ValidationError as e:
        msg = str(e).lower()
        if "bill_no" in msg or "supplier invoice" in msg or "already exist" in msg:
            pi.bill_no = expense_id
            pi.insert(ignore_permissions=True)
            return
        raise


def _build_comment(memo, merchant, card, bcur, bamt, tcur, tamt, posting_date):
    """Build the PI remarks string, skipping fields we have no value for.

    Supplier (Airwallex `merchant`) is null on every Spend Expense for
    this tenant, and Card (CARD_NAME_MAP lookup) is empty until the map
    is populated for a given card_id. Showing "Supplier:" or "Card:"
    with nothing after them is noise, so only include them when they
    actually have a value. Same logic for Original Currency when it
    matches Trans. Value.
    """
    parts = [f"Description: {memo}"]
    if merchant:
        parts.append(f"Supplier: {merchant}")
    if card:
        parts.append(f"Card: {card}")
    parts.append(f"Trans. Value: {bcur} {_money(bamt)}")
    if tcur and (tcur != bcur or _money(tamt) != _money(bamt)):
        parts.append(f"Original Currency: {tcur} {_money(tamt)}")
    parts.append(f"Transaction Date: {format_date(posting_date, 'dd/MM/yyyy')}")
    return "      ".join(parts)


# ----------------------------------------------------------------------------
# Attachments
# ----------------------------------------------------------------------------
def attach_receipts(pi_name, expense, token, api_url):
    """Download receipt files for an expense and attach them to the draft PI.
    Returns the number of files attached. Never raises; logs and continues."""
    attached = 0
    for att in (expense.get("attachments") or []):
        url = att.get("file_url")
        if not url:
            continue
        file_name = att.get("file_name") or f"{att.get('id', 'receipt')}"
        content = _download(url, token)
        if content is None:
            continue
        try:
            frappe.get_doc({
                "doctype": "File",
                "file_name": file_name,
                "attached_to_doctype": "Purchase Invoice",
                "attached_to_name": pi_name,
                "is_private": 1,
                "content": content,
            }).insert(ignore_permissions=True)
            attached += 1
        except Exception as e:
            frappe.log_error(
                message=f"Attach failed for {pi_name} / {file_name}: {str(e)[:300]}",
                title="AirW Expense Attachment Error",
            )
    return attached


def _download(url, token):
    """Fetch a file. Tries with the bearer token, then without (presigned URLs)."""
    for headers in ({"Authorization": f"Bearer {token}"}, {}):
        try:
            r = requests.get(url, headers=headers, timeout=60)
            if r.status_code == 200 and r.content:
                return r.content
        except Exception:
            continue
    frappe.log_error(message=f"Could not download attachment: {url[:200]}",
                     title="AirW Expense Attachment Download")
    return None


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def purchase_invoice_exists(expense_id):
    return bool(frappe.db.exists("Purchase Invoice",
                                 {"custom_tm_airwallex_expense_id": expense_id}))


def _iso_range(settings, from_date, to_date):
    """Return inclusive from / exclusive to ISO8601 strings for the API."""
    if not to_date:
        to_date = today()
    if not from_date:
        from_date = add_days(getdate(to_date), -30)
    from_iso = f"{getdate(from_date).isoformat()}T00:00:00Z"
    # to_created_at is exclusive, so push to the start of the day after to_date
    to_iso = f"{add_days(getdate(to_date), 1).isoformat()}T00:00:00Z"
    return from_iso, to_iso


def _to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _money(value):
    """Format like the manual sheet: 2 dp, trailing zeros trimmed (6.7, 279, 98.02)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{v:.2f}".rstrip("0").rstrip(".")
