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

import frappe
from frappe.utils import get_datetime, getdate

from bank_integration.feed import common

ROLES = ("System Manager", "Accounts Manager")


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


def _range(settings, from_date, to_date):
    """From the start of from_date to the end of to_date, never on or before
    Reconciled Up To."""
    floor = settings.get("reconciled_to")
    from_dt = get_datetime(getdate(from_date)) if from_date else None
    if floor and (not from_dt or getdate(from_dt) <= getdate(floor)):
        if from_dt:
            frappe.throw(
                f"Reconciled up to {frappe.utils.formatdate(floor)}. Pick a From date after that, "
                "or change Reconciled Up To in the settings."
            )
        from_dt = get_datetime(frappe.utils.add_days(getdate(floor), 1))
    if not from_dt:
        frappe.throw("From date is required")
    to_dt = get_datetime(frappe.utils.add_days(getdate(to_date), 1)) if to_date else frappe.utils.now_datetime()
    to_dt = min(to_dt, frappe.utils.now_datetime())
    if to_dt <= from_dt:
        frappe.throw("To must be after From")
    return from_dt, to_dt


def _run(provider, settings, a, b, dry, curs, only_ids=None, add_unsure=None):
    if provider == "airwallex":
        from bank_integration.airwallex.transaction import run_window

        return run_window(settings, a, b, dry_run=dry, lookups=not dry, currencies=curs or None,
                          only_ids=only_ids, add_unsure=add_unsure)
    from bank_integration.skript.skript_transaction import run_window as skript_window

    return skript_window(settings, a, b, dry_run=dry, only_ids=only_ids, add_unsure=add_unsure)


def _ids(value):
    import json

    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    try:
        out = json.loads(value)
    except ValueError:
        out = [x.strip() for x in str(value).split(",")]
    return [x for x in out if x]


@frappe.whitelist()
def catch_up(provider, from_date=None, to_date=None, dry_run=1, confirm=None, currencies=None,
             add=None, add_unsure=None, adopt=None):
    """Dry run: every line the bank has that ERPNext has no id for, each
    judged NEW, ALREADY THERE (naming the line) or UNSURE, with totals by
    month. Apply (dry_run=0, confirm=YES_ADD): adds the NEW lines (all, or
    only those in `add`), UNSURE lines only when in `add_unsure`, and writes
    the bank's id onto the ALREADY THERE lines in `adopt` ({id: line}).
    Lines are judged again in the job, so nothing judged stale is added."""
    _allowed()
    provider = (provider or "").lower()
    if provider not in common.DOCTYPE:
        frappe.throw("provider is airwallex or skript")
    settings = frappe.get_single(common.DOCTYPE[provider])
    from_dt, to_dt = _range(settings, from_date, to_date)
    curs = [c.strip().upper() for c in str(currencies or "").split(",") if c.strip()]

    if str(dry_run) != "0":
        rep = _run(provider, settings, from_dt, to_dt, True, curs)
        counts = {common.NEW: 0, common.THERE: 0, common.UNSURE: 0}
        for line in rep["lines"]:
            counts[line["verdict"]] += 1
        unrouted = {}
        for lg in rep.get("logins") or []:
            for cur, n in lg.get("unrouted", {}).items():
                unrouted[cur] = unrouted.get(cur, 0) + n
        unmapped = {k: v["unmapped"] for k, v in (rep.get("accounts") or {}).items() if v.get("unmapped")}
        return {
            "provider": provider, "dry_run": True,
            "from": str(from_dt), "to": str(to_dt), "floor": str(settings.get("reconciled_to") or ""),
            "counts": counts, "adoptable": sum(1 for x in rep["lines"] if x.get("adopt")),
            "months": [dict(month=m, **v) for m, v in sorted(rep["months"].items())],
            "lines": rep["lines"], "problems": rep.get("problems") or [], "failed": rep.get("failed") or [],
            "unrouted": unrouted, "unmapped": unmapped,
        }

    if confirm != "YES_ADD":
        frappe.throw("To apply, add &confirm=YES_ADD as well as &dry_run=0.")
    adopt_map = adopt
    if isinstance(adopt_map, str):
        import json

        adopt_map = json.loads(adopt_map or "{}")
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
        add=_ids(add) if add is not None else None,
        add_unsure=_ids(add_unsure),
        adopt=adopt_map or {},
        by=frappe.session.user,
    )
    return {"started": "Working in the background. Check again to see what is left."}


def catch_up_job(provider, from_date, to_date, currencies="", add=None, add_unsure=None, adopt=None, by=None):
    """Background catch-up under the feed's lock: adopt first, then add."""
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
    status = "Failed"
    try:
        adopted, refused = [], []
        for tid, line in (adopt or {}).items():
            why = common.adopt_line(line, tid)
            (refused if why else adopted).append({"id": tid, "line": line, "why": why})
        frappe.db.commit()
        rep = _run(provider, settings, get_datetime(from_date), get_datetime(to_date), False, curs,
                   only_ids=add, add_unsure=add_unsure)
        status = "Completed with Errors" if rep["failed"] or refused else "Completed"
        rep.update({"run": "catch-up", "status": status, "by": by, "adopted": adopted, "adopt_refused": refused})
        common.save_report(dt, rep)
        bi_log.create_log(
            f"{provider.title()} catch-up {from_date} to {to_date}: {rep['created']} added, "
            f"{len(adopted)} adopted, {len(rep['failed'])} failed, {len(refused)} not adopted",
            status="Error" if rep["failed"] or refused else "Info",
            provider=provider.title(),
        )
    except Exception:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), f"{provider.title()} catch-up failed")
    finally:
        common.release_lock(dt, status)


@frappe.whitelist()
def possible_doubles(bank_account, from_date, to_date=None):
    """READ ONLY. Pairs of live lines on a Bank Account (or a blank one in its
    currency) with the same amount and direction within 3 days, where at least
    one was made by a feed (has a transaction_id). For checking lines a run
    added before the full duplicate rule was in (17 Nov 26).

      /api/method/bank_integration.feed.tools.possible_doubles?bank_account=AWX AUD&from_date=2026-11-01
    """
    _allowed()
    acc = frappe.db.get_value("Bank Account", bank_account, "account")
    cur = frappe.db.get_value("Account", acc, "account_currency") if acc else None
    rows = frappe.db.sql(
        """select name, date, deposit, withdrawal, ifnull(transaction_id,'') tid,
                  ifnull(bank_account,'') bank_account, docstatus, status, creation,
                  ifnull(allocated_amount,0) allocated, left(ifnull(description,''), 60) description
           from `tabBank Transaction`
           where docstatus < 2 and date between %s and %s
             and (bank_account = %s or (ifnull(bank_account,'') = '' and currency = %s))
           order by date, creation""",
        (frappe.utils.add_days(getdate(from_date), -3),
         frappe.utils.add_days(getdate(to_date or frappe.utils.nowdate()), 3), bank_account, cur),
        as_dict=True,
    )
    pairs = []
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            if (b.date - a.date).days > 3:
                break
            if round(a.deposit, 2) == round(b.deposit, 2) and round(a.withdrawal, 2) == round(b.withdrawal, 2) \
                    and (a.tid or b.tid):
                pairs.append({
                    "amount": a.deposit or -a.withdrawal,
                    "first": {k: str(a[k]) for k in ("name", "date", "tid", "bank_account", "status", "creation", "description")},
                    "second": {k: str(b[k]) for k in ("name", "date", "tid", "bank_account", "status", "creation", "description")},
                    "matched": [x.name for x in (a, b) if x.allocated],
                })
    return {"bank_account": bank_account, "lines": len(rows), "possible_doubles": len(pairs), "pairs": pairs,
            "note": "Two equal purchases close together are real too: check each pair against the statement."}
