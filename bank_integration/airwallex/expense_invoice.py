"""
Airwallex Spend Expenses -> ERPNext Purchase Invoice (DRAFT) importer.

Behaviour
- Pulls expenses in the workflow states listed in IMPORT_STATUSES
  (defaults to APPROVED - the approver has acted as a human checkpoint)
  over a created_at window. Approvers mark per-expense exceptions by
  appending a SKIP_DESCRIPTION_TAGS token (default ``#no_export``) to
  the description before approving; those rows are then skipped.
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
NON_AUD_TAX_TEMPLATE = "FRE - GST FREE (TM)"
LETTER_HEAD = "General TM"
TITLE_PREFIX = "AirW "
GST_DIVISOR = 1.1
# Airwallex Spend Expense statuses to import. The full enum is DRAFT,
# AWAITING_APPROVAL, REJECTED, APPROVED, ARCHIVED. APPROVED is the single
# gateway - the approver has acted as a human checkpoint and only
# approves expenses that should hit ERPNext. Any other filtering happens
# upstream of approval (in Airwallex Spend) so the integration stays
# simple: approved = imported, unapproved = not.
#
# The one opt-out the approver has is to append a SKIP_DESCRIPTION_TAGS
# token to the expense description before approving. See below.
IMPORT_STATUSES = ["APPROVED"]

# Tokens that, if present anywhere in the cardholder description, cause
# the importer to skip the expense even though it is APPROVED. Default
# is "#no_export" - the approver types or appends this when the expense
# should not become a draft PI in ERPNext (e.g. a PO already exists for
# the purchase and the PI will be created from the PO workflow, or it
# was already entered manually). Match is case-insensitive, whole-word.
SKIP_DESCRIPTION_TAGS = {"#no_export"}
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
    # "<card-uuid>": "Cardholder Name",  # (use site_config instead)
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
    """Import expenses in IMPORT_STATUSES (default APPROVED) created
    within the window into draft PIs. Expenses whose description carries
    a SKIP_DESCRIPTION_TAGS token (default ``#no_export``) are skipped."""
    settings = frappe.get_single("Bank Integration Setting")

    if not getattr(settings, "airwallex_clients", None):
        bi_log.create_log("Expense import: no Airwallex clients configured", status="Error")
        return

    from_iso, to_iso = _iso_range(settings, from_date, to_date)

    totals = {
        "scanned": 0, "created": 0, "duplicate": 0, "errors": 0,
        "attachments": 0, "skipped_wrong_status": 0, "skipped_by_tag": 0,
    }
    # Each entry: {expense_id, date, amount, currency, card_name, description}
    skipped_tag_details = []

    for client in settings.airwallex_clients:
        try:
            api = Expenses(
                client_id=client.airwallex_client_id,
                api_key=client.get_password("airwallex_api_key"),
                api_url=settings.api_url,
            )
            token = api.get_valid_token()

            for expense in api.iter_all(
                status=IMPORT_STATUSES,
                from_created_at=from_iso,
                to_created_at=to_iso,
            ):
                totals["scanned"] += 1
                expense_id = expense.get("id")

                if not expense_id:
                    continue

                # Defensive: Airwallex has been known to return rows that
                # don't strictly match the status filter we sent, especially
                # if they have added a new enum value not in our list. Skip
                # anything whose status is not what we asked for.
                if expense.get("status") not in IMPORT_STATUSES:
                    totals["skipped_wrong_status"] += 1
                    continue

                if _description_has_skip_tag(expense.get("description")):
                    totals["skipped_by_tag"] += 1
                    skipped_tag_details.append({
                        "expense_id": expense_id,
                        "date": (expense.get("settled_at") or expense.get("created_at") or "")[:10],
                        "amount": expense.get("billing_amount"),
                        "currency": (expense.get("billing_currency") or "").upper(),
                        "card_name": _card_name_map().get(expense.get("card_id"), ""),
                        "description": expense.get("description"),
                    })
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

    # Plain text for the integration log (searchable, compact).
    log_summary = (
        f"Airwallex expense import complete. "
        f"Scanned {totals['scanned']}, created {totals['created']} draft PIs, "
        f"skipped {totals['duplicate']} already imported, "
        f"skipped {totals['skipped_by_tag']} by description tag, "
        f"skipped {totals['skipped_wrong_status']} wrong status, "
        f"errors {totals['errors']}, attachments {totals['attachments']}."
    )
    bi_log.create_log(log_summary, status="Error" if totals["errors"] else "Success")
    frappe.db.commit()

    # Rich HTML for the popup the user sees in Desk. Uses Bootstrap-
    # compatible classes only (text-muted, table, badge etc.) so the
    # styling adapts to whichever Frappe theme - light or dark - the user
    # has selected.
    html_summary = _build_import_summary_html(totals, skipped_tag_details, from_date, to_date)

    frappe.publish_realtime(
        "expense_import_complete",
        {"status": "error" if totals["errors"] else "success", "message": html_summary},
    )
    return log_summary


def _build_import_summary_html(totals, skipped_tag_details, from_date, to_date):
    """Build the HTML body for the completion msgprint.

    Style targets: Frappe Bootstrap classes only (table, table-sm,
    table-bordered, text-muted, small, code) so the output picks up
    Frappe's CSS variables for both light and dark themes. No inline
    colours, no hardcoded backgrounds.
    """
    from frappe.utils import escape_html

    def _row(label, value, accent=""):
        if accent:
            value = f'<span class="{accent}">{value}</span>'
        return f'<tr><th style="width:60%">{label}</th><td class="text-right">{value}</td></tr>'

    summary_rows = [
        _row("Created draft Purchase Invoices", totals["created"], "text-success"),
        _row("Skipped &mdash; already imported", totals["duplicate"], "text-muted"),
        _row('Skipped &mdash; <code>#no_export</code> tag', totals["skipped_by_tag"], "text-muted"),
        _row("Skipped &mdash; wrong status", totals["skipped_wrong_status"], "text-muted"),
        _row("Errors", totals["errors"], "text-danger" if totals["errors"] else "text-muted"),
        _row("Attachments downloaded", totals["attachments"], "text-muted"),
        _row("Expenses scanned (total)", totals["scanned"], "text-muted"),
    ]

    skipped_section = ""
    if skipped_tag_details:
        rows = []
        for d in skipped_tag_details:
            amount = ""
            try:
                amount = f"{float(d['amount']):.2f}"
            except (TypeError, ValueError):
                amount = escape_html(str(d.get("amount") or ""))
            rows.append(
                "<tr>"
                f"<td>{escape_html(d['date'] or '')}</td>"
                f'<td class="text-right">{escape_html(d["currency"] or "")} {amount}</td>'
                f"<td>{escape_html(d['card_name'] or '')}</td>"
                f"<td>{escape_html(d['description'] or '')}</td>"
                "</tr>"
            )
        skipped_section = f"""
        <h6 style="margin-top:18px;">Skipped by <code>#no_export</code></h6>
        <p class="text-muted small">These approved expenses were intentionally
        excluded from import because the approver tagged them in the
        description. Typical reasons: a PO will create the PI separately,
        or the entry was already made manually.</p>
        <div style="max-height:260px;overflow:auto;">
        <table class="table table-sm table-bordered">
            <thead>
                <tr>
                    <th>Date</th>
                    <th class="text-right">Amount</th>
                    <th>Card</th>
                    <th>Description</th>
                </tr>
            </thead>
            <tbody>{''.join(rows)}</tbody>
        </table>
        </div>
        """

    window_html = ""
    if from_date or to_date:
        window_html = (
            f'<p class="text-muted small" style="margin-bottom:8px;">'
            f"Window: <code>{escape_html(str(from_date or ''))}</code> &rarr; "
            f"<code>{escape_html(str(to_date or ''))}</code></p>"
        )

    return f"""
    <div>
        {window_html}
        <table class="table table-sm table-bordered">
            <tbody>{''.join(summary_rows)}</tbody>
        </table>

        <p class="text-muted small" style="margin-top:8px;">
            <strong>Duplicate protection:</strong> every imported expense
            carries its Airwallex expense ID on the resulting Purchase
            Invoice (<code>custom_tm_airwallex_expense_id</code>). Re-running
            the import over the same or overlapping date range will not
            create duplicate Purchase Invoices &mdash; already-imported
            rows are detected and skipped automatically.
        </p>

        {skipped_section}
    </div>
    """


# ----------------------------------------------------------------------------
# Invoice construction
# ----------------------------------------------------------------------------
def create_draft_invoice(expense):
    """Build and insert one draft Purchase Invoice for an expense."""
    expense_id = expense.get("id")
    merchant = (expense.get("merchant") or "").strip()
    raw_description = (expense.get("description") or "").strip()
    memo = (_strip_skip_tags(raw_description) or merchant or "Airwallex expense").strip()
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

    # Taxes: AUD rows get the 10% GST template, everything else gets the
    # GST Free template so BAS reporting still picks the row up under the
    # right classification. Clear anything inherited from supplier or item
    # defaults first.
    pi.set("taxes", [])
    from erpnext.controllers.accounts_controller import get_taxes_and_charges
    tax_template = AUD_TAX_TEMPLATE if is_aud else NON_AUD_TAX_TEMPLATE
    pi.taxes_and_charges = tax_template
    for row in get_taxes_and_charges("Purchase Taxes and Charges Template", tax_template):
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
def _description_has_skip_tag(description):
    """Whole-word, case-insensitive check for any of SKIP_DESCRIPTION_TAGS
    in the description string."""
    if not description:
        return False
    tokens = {t.lower() for t in description.split()}
    return any(tag.lower() in tokens for tag in SKIP_DESCRIPTION_TAGS)


def _strip_skip_tags(text):
    """Remove SKIP_DESCRIPTION_TAGS tokens from a string. Used before
    putting the cardholder memo into the PI so the tag does not leak
    into bill_no / title / remarks."""
    if not text:
        return text
    skip_lower = {t.lower() for t in SKIP_DESCRIPTION_TAGS}
    kept = [tok for tok in text.split() if tok.lower() not in skip_lower]
    return " ".join(kept).strip()


def purchase_invoice_exists(expense_id):
    return bool(frappe.db.exists("Purchase Invoice",
                                 {"custom_tm_airwallex_expense_id": expense_id}))


def _iso_range(settings, from_date, to_date):
    """Return inclusive from / exclusive to ISO8601 strings for the API.

    Treats the user-supplied date as local-time midnight (per the site's
    system timezone), then converts to UTC for the Airwallex API. Without
    this conversion the window slid by ~10 hours for AU sites because
    midnight UTC is 10am Melbourne, so picking "from 2026-05-01" missed
    spend that happened in the first 10 hours of May 1 local time.
    """
    import pytz
    from datetime import datetime, time

    if not to_date:
        to_date = today()
    if not from_date:
        from_date = add_days(getdate(to_date), -30)

    try:
        tz = pytz.timezone(frappe.utils.get_system_timezone())
    except Exception:
        tz = pytz.UTC

    from_dt = tz.localize(datetime.combine(getdate(from_date), time.min))
    # to_created_at is exclusive, so push to the start of the day after to_date
    to_dt = tz.localize(datetime.combine(add_days(getdate(to_date), 1), time.min))

    def _to_z(dt):
        return dt.astimezone(pytz.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    return _to_z(from_dt), _to_z(to_dt)


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
