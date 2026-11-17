"""Skript bank feed (CBA, Judo) into Bank Transaction.

Rewritten 17 Nov 26 with the Airwallex feed (see airwallex/transaction.py
and feed/common.py for the rules): look-back window, a savepoint per line,
failures kept and not skipped past, a stale lock taken over, every page read.
"""
import frappe
from bank_integration.skript.api.skript_transactions_api import SkriptTransactions
from bank_integration.skript.api.skript_base_api import SkriptAPIError
from bank_integration.skript.skript_utils import map_skript_to_erpnext, format_datetime_for_skript_filter , parse_skript_to_system_timezone
from frappe.utils import get_datetime

from bank_integration.bank_integration.doctype.bank_integration_log import bank_integration_log as bi_log
from bank_integration.feed import common


DT = common.SKRIPT
CURSOR_F = common.CURSOR_F
PAGE_SIZE = 1000
MAX_PAGES = 100


def _items(response):
    if isinstance(response, list):
        return response
    if isinstance(response, dict):
        data = response.get("items")
        if data is None:
            data = response.get("data")
        if isinstance(data, dict):
            data = data.get("transactions") or data.get("items") or []
        return data or []
    return []


def _next_ref(response):
    if not isinstance(response, dict):
        return None
    for key in ("ref", "nextRef", "next_ref"):
        if response.get(key):
            return response[key]
    links = response.get("links") or {}
    if isinstance(links, dict) and links.get("next"):
        nxt = str(links["next"])
        if "ref=" in nxt:
            return nxt.split("ref=", 1)[1].split("&", 1)[0]
    meta = response.get("meta") or {}
    if isinstance(meta, dict):
        for key in ("ref", "nextRef", "next"):
            if meta.get(key):
                return meta[key]
    return None


def fetch_window(api, from_dt, to_dt):
    """Every Skript transaction posted in [from_dt, to_dt], across all pages.

    Skript pages with a ref when it gives one. When it gives none and a page
    comes back with anything new, the next call asks again from the latest
    posting time seen (ties caught by the id check); it stops when a call
    brings nothing new.
    """
    from_s = format_datetime_for_skript_filter(from_dt)
    to_s = format_datetime_for_skript_filter(to_dt)
    lower = from_s
    seen = {}
    ref = None
    for _ in range(MAX_PAGES):
        expr = f"postingDateTime BETWEEN {{ts '{lower}'}} AND {{ts '{to_s}'}}"
        response = api.get_list_all(filter=expr, size=PAGE_SIZE, ref=ref)
        batch = _items(response)
        fresh = [t for t in batch if t.get("id") not in seen]
        for t in fresh:
            seen[t.get("id")] = t
        if not batch or not fresh:
            break
        ref = _next_ref(response)
        if ref:
            continue
        # No ref: ask again from the latest posting time seen. Skript may
        # cap a page below what was asked, so a short page is not proof of
        # the end; a page with nothing new is. Costs one extra call a run.
        latest = max(parse_skript_to_system_timezone(t.get("postingDateTime")) for t in batch)
        lower = format_datetime_for_skript_filter(latest)
    else:
        raise SkriptAPIError(f"Stopped after {MAX_PAGES} pages; narrow the date range", 500)
    return list(seen.values())


def run_window(settings, from_dt, to_dt, dry_run=False):
    """Bring in the Skript lines posted in the window that ERPNext lacks.

    Each line on its own savepoint; failures kept in the report. Lines on a
    Skript account with no ERPNext Bank Account are counted, not an error
    for the rest (before 17 Nov 26 one unmapped account stopped the lot).
    """
    report = {
        "window": [str(from_dt), str(to_dt)],
        "dry_run": bool(dry_run),
        "problems": [],
        "accounts": {},
        "failed": [],
        "would_add": [],
        "created": 0,
    }
    account_map = {}
    names = {}
    for row in settings.accounts or []:
        names[row.account_id] = row.display_name or row.masked_number or row.account_id
        if row.bank_account:
            account_map[row.account_id] = row.bank_account
        else:
            report["problems"].append(f"Skript account {names[row.account_id]} has no Bank Account")

    api = SkriptTransactions(
        consumer_id=settings.consumer_id,
        client_id=settings.get_password("client_id"),
        client_secret=settings.get_password("client_secret"),
        api_url=settings.api_url,
        api_scope=settings.api_scope,
    )
    lines = fetch_window(api, from_dt, to_dt)
    have = common.existing_ids([t.get("id") for t in lines])

    for n, txn in enumerate(lines):
        acc = txn.get("accountId")
        label = names.get(acc) or acc or "none"
        part = report["accounts"].setdefault(label, {
            "bank_account": account_map.get(acc), "fetched": 0, "existing": 0,
            "created": 0, "failed": 0, "unmapped": 0, "last_posted": None,
        })
        part["fetched"] += 1
        posted = txn.get("postingDateTime")
        if posted and (not part["last_posted"] or str(posted) > str(part["last_posted"])):
            part["last_posted"] = posted
        if txn.get("id") in have:
            part["existing"] += 1
            continue
        bank_account = account_map.get(acc)
        if not bank_account:
            part["unmapped"] += 1
            continue
        brief = {
            "id": txn.get("id"), "when": parse_skript_to_system_timezone(posted),
            "amount": txn.get("amount"), "description": (txn.get("description") or "")[:120],
            "bank_account": bank_account,
        }
        if dry_run:
            report["would_add"].append(brief)
            continue
        try:
            doc = map_skript_to_erpnext(txn, bank_account)
        except Exception as e:  # noqa: BLE001
            doc, fail = None, common.clean_reason(e)
        else:
            name, fail = common.insert_line(doc, f"skript_line_{n}")
            if name:
                part["created"] += 1
                report["created"] += 1
                if report["created"] % common.COMMIT_EVERY == 0:
                    frappe.db.commit()
                continue
        part["failed"] += 1
        brief["reason"] = fail
        report["failed"].append(brief)
    return report


def _finish(report, run, started, status):
    report["run"] = run
    report["started"] = str(started)
    report["ended"] = str(frappe.utils.now_datetime())
    report["status"] = status
    common.save_report(DT, report)


def _claim(run):
    try:
        note = common.claim_lock(DT, "Skript")
    except common.FeedBusy as e:
        frappe.logger("bank_integration").info(str(e))
        return False
    if note:
        bi_log.create_log(note, status="Error", provider="Skript")
    return True


def sync_skript_transactions(setting_name=None, from_date=None, to_date=None, run="sync old", **kwargs):
    """A manual range (the Skript "Sync old" button, or catch-up). Takes the
    same lock as the scheduled run and never moves the scheduled start."""
    settings = frappe.get_single(DT)
    if not settings.enabled:
        return None
    if not _claim(run):
        return {"busy": True}
    started = frappe.utils.now_datetime()
    status = "Failed"
    report = None
    try:
        report = run_window(settings, get_datetime(from_date), get_datetime(to_date or started))
        status = "Completed with Errors" if report["failed"] else "Completed"
        _finish(report, run, started, status)
        _log_summary(run, report)
    except Exception:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), f"Skript {run} failed")
    finally:
        common.release_lock(DT, status)
    return report


def sync_scheduled_transactions_skript(setting_name=None, schedule_type=None, force=False):
    """The scheduled run: window from the last good run less the look-back.

    One window for every account. The old code moved one cursor to the
    latest posting time on ANY account, so an account that posts late (Judo)
    lost every line posted before CBA's latest; the look-back covers that.
    """
    settings = frappe.get_single(DT)
    if not settings.enabled or (not force and settings.sync_schedule != schedule_type):
        return
    if not _claim("scheduled run"):
        return
    started = frappe.utils.now_datetime()
    status = "Failed"
    try:
        first_days = {"Hourly": 1, "Daily": 2, "Weekly": 8, "Monthly": 31}.get(schedule_type, 1)
        from_dt = common.window_start(settings.get(CURSOR_F), first_days)
        report = run_window(settings, from_dt, started)
        status = "Completed with Errors" if report["failed"] else "Completed"
        cursor = common.next_cursor(started, report["failed"], settings.get(CURSOR_F))
        common.set_single(DT, CURSOR_F, cursor)
        _finish(report, "sync now" if force else "scheduled", started, status)
        if report["created"] or report["failed"]:
            _log_summary("scheduled run", report)
    except Exception:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "Skript scheduled sync failed")
    finally:
        common.release_lock(DT, status)


def _log_summary(run, report):
    fails = len(report["failed"])
    bi_log.create_log(
        f"Skript {run}: {report['created']} added, {fails} failed, "
        f"window {report['window'][0]} to {report['window'][1]}",
        status="Error" if fails else "Info",
        provider="Skript",
    )


def transaction_exists(transaction_id):
    """True when a Bank Transaction carries this transaction_id."""
    return frappe.db.exists("Bank Transaction", {"transaction_id": transaction_id})
