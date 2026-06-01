"""One-shot backfills for the Airwallex integration.

Two whitelisted endpoints, both idempotent and both safe to re-run:

  backfill_reference_numbers(from_date, to_date, dry_run=0)
    Re-resolves Bank Transaction reference_number for already-imported
    rows. Used when the resolver logic changes (e.g. a new mapping rule
    is added) and existing rows need to catch up.

  link_existing_purchase_invoices(from_date, to_date, dry_run=1, force_relink=0)
    Finds manual Purchase Invoices (no custom_tm_airwallex_expense_id)
    that match Airwallex Spend Expenses by date + amount + currency, and
    backfills the link so the expense importer's idempotency picks them
    up going forward. Use this once to migrate from manual entry to the
    auto-importer without creating duplicates.

Both endpoints require the Accounts Manager role.
"""

import frappe

from bank_integration.airwallex.api.expenses import Expenses
from bank_integration.airwallex.api.financial_transactions import FinancialTransactions
from bank_integration.airwallex.expense_invoice import (
    IMPORT_STATUSES,
    SUPPLIER,
    _iso_range as _expense_iso_range,
    _iso_to_local_date,
)
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


@frappe.whitelist()
def link_existing_purchase_invoices(
    from_date,
    to_date,
    dry_run=1,
    client_index=0,
    force_relink=0,
):
    """Link existing manual Purchase Invoices to their Airwallex Spend
    Expense by writing custom_tm_airwallex_expense_id on the PI.

    Use case: you have manually-typed PIs from before the auto importer
    was running. After this migration, the auto importer's idempotency
    will recognise those rows and skip them rather than creating
    duplicates.

    Match rules (conservative):
        supplier == SUPPLIER  ("Airwallex Expense")
        docstatus < 2          (skip cancelled)
        same currency
        grand_total == billing_amount  (within 0.01)
        posting_date == settled_at YYYY-MM-DD (also tries +/- 1 day)
        exactly one PI candidate at the key

    Multiple PI candidates at a key  -> "ambiguous", reported, not written.
    No candidate at any of the three date offsets -> "no_match".
    PI already linked to a *different* expense_id -> skipped, unless
    force_relink=1.

    Args:
        from_date / to_date: ISO date strings (YYYY-MM-DD).
        dry_run (default 1): set to 0 to actually write. Always start
            with dry_run=1, eyeball the sample list, then run for real.
        client_index: which Airwallex client row to use for credentials.
            Same Airwallex tenant returns the same expenses regardless
            of which client credential is used, so index 0 is fine.
        force_relink: pass 1 to overwrite PIs that already have a
            different custom_tm_airwallex_expense_id set. Default 0 is
            the safe behaviour - never replace an existing link.

    Returns:
        dict with counters, including a samples list (max 30 entries)
        showing one example of each outcome class so you can audit the
        matching quality before committing.
    """
    if "Accounts Manager" not in frappe.get_roles():
        frappe.throw(
            "Only Accounts Managers can run the PI backfill.",
            frappe.PermissionError,
        )

    dry_run = bool(int(dry_run))
    force_relink = bool(int(force_relink))

    settings = frappe.get_single("Bank Integration Setting")
    if not settings.airwallex_clients:
        return {"error": "no Airwallex clients configured"}

    client = settings.airwallex_clients[int(client_index)]
    api_url = settings.api_url
    from_iso, to_iso = _expense_iso_range(settings, from_date, to_date)

    # Pull candidate PIs once. Index by (posting_date, grand_total, currency).
    pi_filters = {
        "supplier": SUPPLIER,
        "posting_date": ["between", [from_date, to_date]],
        "docstatus": ["<", 2],
    }
    if not force_relink:
        pi_filters["custom_tm_airwallex_expense_id"] = ["in", ["", None]]

    candidate_pis = frappe.get_all(
        "Purchase Invoice",
        filters=pi_filters,
        fields=[
            "name", "posting_date", "grand_total", "currency",
            "bill_no", "custom_tm_airwallex_expense_id",
        ],
    )

    by_key = {}
    for pi in candidate_pis:
        try:
            amount = round(float(pi.grand_total or 0), 2)
        except (TypeError, ValueError):
            continue
        key = (str(pi.posting_date), amount, (pi.currency or "").upper())
        by_key.setdefault(key, []).append(pi)

    # Pre-compute the set of expense_ids already linked to ANY Purchase
    # Invoice in the window (typically the auto-imported ones, which are
    # excluded from candidate_pis by the filter above). Used to split
    # the "no_match" bucket into "already auto-linked elsewhere" vs
    # "truly no PI exists for this expense yet".
    already_linked_expense_ids = set()
    for row in frappe.get_all(
        "Purchase Invoice",
        filters={
            "posting_date": ["between", [from_date, to_date]],
            "custom_tm_airwallex_expense_id": ["not in", ["", None]],
            "docstatus": ["<", 2],
        },
        fields=["custom_tm_airwallex_expense_id"],
    ):
        eid = row.get("custom_tm_airwallex_expense_id")
        if eid:
            already_linked_expense_ids.add(eid)

    expenses_api = Expenses(
        client_id=client.airwallex_client_id,
        api_key=client.get_password("airwallex_api_key"),
        api_url=api_url,
    )

    results = {
        "scanned_expenses": 0,
        "candidate_pis_loaded": len(candidate_pis),
        "linked": 0,
        "ambiguous": 0,
        "no_match": 0,
        "no_match_breakdown": {
            "already_imported_by_auto": 0,
            "no_pi_exists": 0,
        },
        "skipped_already_linked": 0,
        "relink_collision": 0,
        "samples": [],
        "dry_run": dry_run,
        "force_relink": force_relink,
    }

    # Cap samples per outcome class so a flood of one type does not
    # crowd out visibility of rarer outcomes (e.g. ambiguous /
    # relink_collision are exactly the ones worth seeing).
    SAMPLE_CAP_PER_OUTCOME = 8
    per_outcome_count = {}

    def _sample(outcome, **kwargs):
        used = per_outcome_count.get(outcome, 0)
        if used >= SAMPLE_CAP_PER_OUTCOME:
            return
        per_outcome_count[outcome] = used + 1
        results["samples"].append({"outcome": outcome, **kwargs})

    from frappe.utils import add_days, getdate

    seen_expense_ids = set()

    for expense in expenses_api.iter_all(
        from_created_at=from_iso,
        to_created_at=to_iso,
    ):
        results["scanned_expenses"] += 1
        expense_id = expense.get("id")
        if not expense_id or expense_id in seen_expense_ids:
            continue
        seen_expense_ids.add(expense_id)

        date_str = _iso_to_local_date(
            expense.get("settled_at") or expense.get("created_at")
        )
        try:
            amount = round(abs(float(expense.get("billing_amount") or 0)), 2)
        except (TypeError, ValueError):
            continue
        currency = (expense.get("billing_currency") or "").upper()

        if not (date_str and amount and currency):
            continue

        # Exact-day match first; then +/- 1 day for timezone slip.
        candidates = list(by_key.get((date_str, amount, currency), []))
        if not candidates:
            for offset in (-1, 1):
                try:
                    alt = str(add_days(getdate(date_str), offset))
                except Exception:
                    continue
                candidates = list(by_key.get((alt, amount, currency), []))
                if candidates:
                    date_str = alt
                    break

        if not candidates:
            results["no_match"] += 1
            if expense_id in already_linked_expense_ids:
                outcome = "no_match_already_imported_by_auto"
                results["no_match_breakdown"]["already_imported_by_auto"] += 1
            else:
                outcome = "no_match_no_pi_exists"
                results["no_match_breakdown"]["no_pi_exists"] += 1
            _sample(
                outcome,
                expense_id=expense_id,
                date=date_str,
                amount=amount,
                currency=currency,
                description=expense.get("description"),
            )
            continue

        if len(candidates) > 1:
            results["ambiguous"] += 1
            _sample(
                "ambiguous",
                expense_id=expense_id,
                date=date_str,
                amount=amount,
                currency=currency,
                description=expense.get("description"),
                candidates=[pi.name for pi in candidates],
            )
            continue

        pi = candidates[0]
        existing_link = pi.custom_tm_airwallex_expense_id or ""

        if existing_link == expense_id:
            # Already linked to this exact expense - no-op.
            results["skipped_already_linked"] += 1
            continue

        if existing_link and not force_relink:
            # Linked to a different expense_id - default safe behaviour
            # is to leave it alone.
            results["skipped_already_linked"] += 1
            _sample(
                "skipped_already_linked",
                expense_id=expense_id,
                pi=pi.name,
                existing_expense_id=existing_link,
            )
            continue

        # Write (or count as would-write in dry run).
        if not dry_run:
            try:
                frappe.db.set_value(
                    "Purchase Invoice",
                    pi.name,
                    "custom_tm_airwallex_expense_id",
                    expense_id,
                    update_modified=False,
                )
            except Exception as e:
                # Most likely the unique constraint - some other PI
                # already carries this expense_id (i.e. you have a
                # duplicate-PI situation in ERPNext to investigate).
                results["relink_collision"] += 1
                _sample(
                    "relink_collision",
                    expense_id=expense_id,
                    pi=pi.name,
                    error=str(e)[:200],
                )
                continue
        results["linked"] += 1
        _sample(
            "linked",
            expense_id=expense_id,
            pi=pi.name,
            date=date_str,
            amount=amount,
            currency=currency,
            description=expense.get("description"),
            was_previously_linked_to=existing_link or None,
        )

        # Remove this PI from the index so a later expense at the same
        # key cannot match it again.
        key = (str(pi.posting_date), amount, (pi.currency or "").upper())
        if key in by_key:
            try:
                by_key[key].remove(pi)
                if not by_key[key]:
                    del by_key[key]
            except ValueError:
                pass

    if not dry_run:
        frappe.db.commit()
    return results
