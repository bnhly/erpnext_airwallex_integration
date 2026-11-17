"""Read-only feed check, and the catch-up action.

feed_check (System Manager or Accounts Manager), READ ONLY:
  /api/method/bank_integration.feed.tools.feed_check
  /api/method/bank_integration.feed.tools.feed_check?txn=dbe76514-...&ask_airwallex=1
Answers: how each Airwallex row routes (Bank Account, ledger account and its
currency), whether Bank Transaction's Bank Account was made mandatory and by
whom, lines per bank account, lines with no bank account, drafts left behind,
the feed's Error Log titles since a date, the log backlog, and the last run
reports. With txn= it says whether that line is in ERPNext; with
ask_airwallex=1 it also asks Airwallex for that one line (one GET).

catch_up (System Manager or Accounts Manager):
  /api/method/bank_integration.feed.tools.catch_up?provider=airwallex&from_date=2025-07-01&to_date=2026-11-17
      dry run: lists what would be added, writes nothing
  ...&currencies=AUD                     only those currencies (Airwallex)
  ...&dry_run=0&confirm=YES_ADD          add them (background job)
Lines already in ERPNext (by transaction_id, cancelled ones included) are
never added again.
"""

from datetime import timedelta

import frappe
from frappe.utils import get_datetime, getdate

from bank_integration.feed import common

ROLES = ("System Manager", "Accounts Manager")
SLICE_DAYS = 31


def _allowed():
    if frappe.session.user == "Administrator":
        return
    if not set(ROLES) & set(frappe.get_roles()):
        frappe.throw("Only a System Manager or Accounts Manager can run this.", frappe.PermissionError)


@frappe.whitelist()
def feed_check(txn=None, ask_airwallex=0, errors_since="2026-10-25"):
    _allowed()
    from bank_integration.airwallex.transaction import client_routes

    s = frappe.get_single(common.AIRWALLEX)
    k = frappe.get_single(common.SKRIPT)
    out = {"settings": {}, "airwallex_rows": [], "airwallex_problems": []}
    for label, doc in (("airwallex", s), ("skript", k)):
        out["settings"][label] = {
            f: (str(doc.get(f)) if doc.get(f) is not None else None)
            for f in ("enabled", "sync_schedule", "sync_status", "sync_started_on", "last_sync_date", "enable_log")
        }

    for row in s.clients or []:
        acc = frappe.db.get_value("Bank Account", row.bank_account, "account") if row.bank_account else None
        out["airwallex_rows"].append({
            "row": row.idx,
            "client": (row.airwallex_client_id or "")[:8],
            "bank_account": row.bank_account,
            "ledger_account": acc,
            "account_currency": frappe.db.get_value("Account", acc, "account_currency") if acc else None,
        })
    logins, problems = client_routes(s)
    out["airwallex_problems"] = problems
    out["airwallex_routes"] = [
        {"client": lg["client_id"][:8], "routes": {c: r.bank_account for c, r in lg["routes"].items()}}
        for lg in logins
    ]
    out["skript_accounts"] = [
        {"account": r.display_name or r.masked_number or r.account_id, "bank_account": r.bank_account}
        for r in k.accounts or []
    ]

    # Was Bank Account made mandatory on Bank Transaction, and when?
    out["bank_account_mandatory"] = frappe.db.sql(
        """select name, property, value, owner, modified_by, creation, modified
           from `tabProperty Setter`
           where doc_type='Bank Transaction' and field_name='bank_account'""",
        as_dict=True,
    )

    feed_accounts = sorted({r.bank_account for r in s.clients or [] if r.bank_account}
                           | {r.bank_account for r in k.accounts or [] if r.bank_account})
    out["lines_by_bank_account"] = frappe.db.sql(
        """select bank_account, docstatus, count(*) n_lines, max(date) last_date,
                  max(creation) last_made
           from `tabBank Transaction`
           where ifnull(transaction_id,'')!='' and (bank_account in %s or ifnull(bank_account,'')='')
           group by bank_account, docstatus order by bank_account, docstatus""",
        (tuple(feed_accounts) or ("",),),
        as_dict=True,
    )
    out["no_bank_account_by_currency"] = frappe.db.sql(
        """select currency, docstatus, count(*) n_lines, min(date) first_date, max(date) last_date
           from `tabBank Transaction`
           where ifnull(bank_account,'')='' and ifnull(transaction_id,'')!=''
           group by currency, docstatus""",
        as_dict=True,
    )

    from bank_integration.feed.maintenance import _error_where

    where, args = _error_where()
    out["feed_error_log_since"] = frappe.db.sql(
        f"""select method title, count(*) n, min(creation) first, max(creation) last
            from `tabError Log` where creation >= %s and {where}
            group by method order by n desc limit 30""",
        [getdate(errors_since)] + args,
        as_dict=True,
    )
    out["latest_txn_errors"] = frappe.db.sql(
        f"""select creation, method title, left(error, 300) error
            from `tabError Log` where creation >= %s and {where}
            order by creation desc limit 15""",
        [getdate(errors_since)] + args,
        as_dict=True,
    )
    out["bank_integration_log_rows"] = frappe.db.count("Bank Integration Log")
    out["last_airwallex_report"] = common.report_of(common.AIRWALLEX)
    out["last_skript_report"] = common.report_of(common.SKRIPT)

    if txn:
        out["txn"] = {
            "id": txn,
            "in_erpnext": frappe.db.get_all(
                "Bank Transaction",
                filters={"transaction_id": txn},
                fields=["name", "docstatus", "bank_account", "date", "deposit", "withdrawal", "currency"],
            ),
        }
        if str(ask_airwallex) == "1" and logins:
            from bank_integration.airwallex.api.base_api import AirwallexAPIError
            from bank_integration.airwallex.api.financial_transactions import FinancialTransactions

            lg = logins[0]
            try:
                api = FinancialTransactions(
                    client_id=lg["client_id"],
                    api_key=lg["key_row"].get_password("airwallex_api_key"),
                    api_url=s.api_url,
                )
                got = api.get_by_id(txn) or {}
                cur = (got.get("currency") or "").upper()
                route = lg["routes"].get(cur)
                out["txn"]["airwallex"] = {
                    k: got.get(k)
                    for k in ("created_at", "currency", "net", "status", "transaction_type", "source_type")
                }
                out["txn"]["would_go_to"] = route.bank_account if route else None
            except AirwallexAPIError as e:
                out["txn"]["airwallex_error"] = str(e.message)[:300]
    return out


def _slices(from_dt, to_dt):
    cur = from_dt
    while cur < to_dt:
        nxt = min(cur + timedelta(days=SLICE_DAYS), to_dt)
        yield cur, nxt
        cur = nxt


@frappe.whitelist()
def catch_up(provider, from_date, to_date=None, dry_run=1, confirm=None, currencies=None):
    """List (dry run) or add the lines missing from a date range."""
    _allowed()
    provider = (provider or "").lower()
    if provider not in ("airwallex", "skript"):
        frappe.throw("provider is airwallex or skript")
    from_dt = get_datetime(from_date)
    to_dt = get_datetime(to_date) if to_date else frappe.utils.now_datetime()
    if to_dt <= from_dt:
        frappe.throw("to_date must be after from_date")
    curs = [c.strip().upper() for c in str(currencies or "").split(",") if c.strip()]

    if str(dry_run) != "0":
        settings = frappe.get_single(common.DOCTYPE[provider])
        total = {"provider": provider, "dry_run": True, "would_add": [], "failed": [],
                 "problems": [], "unrouted": {}, "slices": 0}
        for a, b in _slices(from_dt, to_dt):
            rep = _run(provider, settings, a, b, True, curs)
            total["slices"] += 1
            total["would_add"] += rep["would_add"]
            total["failed"] += rep["failed"]
            for p in rep.get("problems") or []:
                if p not in total["problems"]:
                    total["problems"].append(p)
            for lg in rep.get("logins") or []:
                for cur, n in lg.get("unrouted", {}).items():
                    total["unrouted"][cur] = total["unrouted"].get(cur, 0) + n
        total["count"] = len(total["would_add"])
        total["by_bank_account"] = {}
        for line in total["would_add"]:
            key = line.get("bank_account")
            total["by_bank_account"][key] = total["by_bank_account"].get(key, 0) + 1
        total["next"] = "Check the list, then add &dry_run=0&confirm=YES_ADD to add them."
        return total

    if confirm != "YES_ADD":
        frappe.throw("To add the lines, add &confirm=YES_ADD as well as &dry_run=0.")
    frappe.enqueue(
        "bank_integration.feed.tools.catch_up_job",
        queue="long",
        timeout=7200,
        job_id=f"bank_feed_catch_up_{provider}",
        deduplicate=True,
        provider=provider,
        from_date=str(from_dt),
        to_date=str(to_dt),
        currencies=",".join(curs),
        by=frappe.session.user,
    )
    return {"started": "Adding in the background. Run the dry run again to see what is left."}


def _run(provider, settings, a, b, dry, curs):
    if provider == "airwallex":
        from bank_integration.airwallex.transaction import run_window

        return run_window(settings, a, b, dry_run=dry, lookups=not dry, currencies=curs or None)
    from bank_integration.skript.skript_transaction import run_window as skript_window

    return skript_window(settings, a, b, dry_run=dry)


def catch_up_job(provider, from_date, to_date, currencies="", by=None):
    """Background catch-up: the range in month slices, under the feed's lock."""
    curs = [c for c in currencies.split(",") if c]
    dt = common.DOCTYPE[provider]
    from bank_integration.bank_integration.doctype.bank_integration_log import bank_integration_log as bi_log

    try:
        note = common.claim_lock(dt, provider.title())
    except common.FeedBusy as e:
        bi_log.create_log(f"Catch-up not started: {e}", status="Error", provider=provider.title())
        return
    if note:
        bi_log.create_log(note, status="Error", provider=provider.title())
    settings = frappe.get_single(dt)
    added, failed, status = 0, [], "Failed"
    try:
        for a, b in _slices(get_datetime(from_date), get_datetime(to_date)):
            rep = _run(provider, settings, a, b, False, curs)
            added += rep["created"]
            failed += rep["failed"]
            frappe.db.commit()
        status = "Completed with Errors" if failed else "Completed"
        report = {"run": "catch-up", "window": [from_date, to_date], "created": added,
                  "failed": failed, "status": status, "by": by}
        common.save_report(dt, report)
        bi_log.create_log(
            f"{provider.title()} catch-up {from_date} to {to_date}: {added} added, {len(failed)} failed",
            status="Error" if failed else "Info",
            provider=provider.title(),
        )
    except Exception:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), f"{provider.title()} catch-up failed")
    finally:
        common.release_lock(dt, status)
