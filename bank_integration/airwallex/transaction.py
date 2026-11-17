"""Airwallex bank feed: financial transactions into Bank Transaction.

Rewritten 17 Nov 26 after lines went missing. What changed, and why:

1. A line that failed to insert was logged and dropped: the run still said
   "Completed" and the next run started after it. Now each line goes in on
   its own savepoint, failures are kept in the run report, the run ends
   "Completed with Errors", and the next window does not start after the
   earliest failure (feed/common.py).
2. Each run asked from the moment the last run FINISHED, so lines created
   while a run was going, or that Airwallex lists late, were never asked
   for. Now every run looks back LOOKBACK_DAYS; lines already in ERPNext are
   skipped by transaction_id.
3. Only the first page of the list was read. Now every page is.
4. "In Progress" left by a run that died blocked every later run. Now a run
   older than STALE_HOURS is taken over.
5. The AUD and USD client rows share one login, so each fetched the same
   lines and the USD row tried to insert every AUD line with no bank account
   (refused once Bank Account became mandatory, which flooded Error Log).
   Now ONE fetch per login, and each line goes to the row whose Bank
   Account's ledger currency matches. Lines in a currency with no row
   (GBP, EUR, SGD wallets) are counted, not inserted, not errors.
6. "Sync old transactions" set the scheduled start point to now, skipping
   everything since the last scheduled run. A manual or catch-up run no
   longer moves the scheduled start point.
"""

import frappe
from frappe.utils import get_datetime, now_datetime

from bank_integration.airwallex.api.base_api import AirwallexAPIError
from bank_integration.airwallex.api.financial_transactions import FinancialTransactions
from bank_integration.airwallex.utils import build_expense_merchant_index, map_airwallex_to_erpnext
from bank_integration.bank_integration.doctype.bank_integration_log import bank_integration_log as bi_log
from bank_integration.feed import common

DT = common.AIRWALLEX
CURSOR_F = common.CURSOR_F

CARD_TYPES = ("CARD_PURCHASE", "CARD_REFUND")


# ------------------------------------------------------------------ routing

def client_routes(settings):
    """Group the Airwallex Client rows by login and route by currency.

    Returns (logins, problems). logins = [{client_id, key_row, routes:
    {currency: row}}]. problems = plain words about rows that cannot route.
    """
    logins = {}
    problems = []
    for row in settings.clients or []:
        cid = (row.airwallex_client_id or "").strip()
        if not cid:
            problems.append(f"Row {row.idx}: no client ID")
            continue
        login = logins.setdefault(cid, {"client_id": cid, "key_row": row, "routes": {}})
        if not row.bank_account:
            problems.append(f"Row {row.idx}: no Bank Account")
            continue
        account = frappe.db.get_value("Bank Account", row.bank_account, "account")
        currency = frappe.db.get_value("Account", account, "account_currency") if account else None
        if not account:
            problems.append(f"Row {row.idx}: Bank Account {row.bank_account} has no ledger account")
            continue
        if not currency:
            problems.append(f"Row {row.idx}: ledger account {account} has no Account Currency")
            continue
        if currency in login["routes"]:
            first = login["routes"][currency]
            problems.append(
                f"Row {row.idx}: {currency} already goes to {first.bank_account} (row {first.idx}); ignored"
            )
            continue
        login["routes"][currency] = row
    return list(logins.values()), problems


def _short(cid):
    return (cid or "unknown")[:8]


# ------------------------------------------------------------------ core run

def run_window(settings, from_dt, to_dt, dry_run=False, lookups=True, currencies=None):
    """Fetch every line in [from_dt, to_dt] for every login and bring in
    the ones ERPNext does not have.

    dry_run: nothing is written; the report lists what would be added.
    lookups: fetch counterparty / merchant names (off for dry runs).
    currencies: only these currencies (catch-up); others are left alone.
    Returns a report dict.
    """
    from_iso = settings._to_iso8601(from_dt)
    to_iso = settings._to_iso8601(to_dt)
    logins, problems = client_routes(settings)
    if currencies:
        keep = {c.upper() for c in currencies}
        for login in logins:
            login["routes"] = {c: r for c, r in login["routes"].items() if c in keep}
    report = {
        "window": [str(from_dt), str(to_dt)],
        "dry_run": bool(dry_run),
        "problems": problems,
        "logins": [],
        "failed": [],
        "would_add": [],
        "by_hand": [],
        "created": 0,
    }
    hand = common.HandLines()

    for login in logins:
        row = login["key_row"]
        part = {
            "client": _short(login["client_id"]),
            "routes": {cur: r.bank_account for cur, r in login["routes"].items()},
            "fetched": 0,
            "existing": 0,
            "filtered": 0,
            "created": 0,
            "unrouted": {},
            "failed": 0,
        }
        report["logins"].append(part)
        if not login["routes"]:
            continue
        try:
            api = FinancialTransactions(
                client_id=login["client_id"],
                api_key=row.get_password("airwallex_api_key"),
                api_url=settings.api_url,
            )
            lines = list(api.iter_all(from_created_at=from_iso, to_created_at=to_iso))
        except AirwallexAPIError as e:
            part["error"] = f"Airwallex said: {str(e.message)[:300]}"
            report["failed"].append({
                "id": None, "when": str(from_dt), "reason": part["error"], "client": part["client"],
            })
            continue

        part["fetched"] = len(lines)
        have = common.existing_ids([t.get("id") for t in lines])
        todo = []
        for txn in lines:
            if txn.get("id") in have:
                part["existing"] += 1
                continue
            ttype = (txn.get("transaction_type") or "").upper()
            if not settings.should_sync_transaction(ttype):
                part["filtered"] += 1
                continue
            cur = (txn.get("currency") or "").upper()
            if cur not in login["routes"]:
                part["unrouted"][cur or "none"] = part["unrouted"].get(cur or "none", 0) + 1
                continue
            todo.append(txn)

        # Merchant names only when there is a new card line to name.
        expense_index = []
        issuing_cache = {}
        if lookups and not dry_run and any(t.get("source_type") in CARD_TYPES for t in todo):
            expense_index = build_expense_merchant_index(row, settings.api_url, from_iso, to_iso)

        for n, txn in enumerate(todo):
            target = login["routes"][(txn.get("currency") or "").upper()]
            fail = None
            try:
                doc = map_airwallex_to_erpnext(
                    txn,
                    target.bank_account,
                    client=target if (lookups and not dry_run) else None,
                    api_url=settings.api_url,
                    expense_index=expense_index,
                    issuing_cache=issuing_cache,
                )
            except Exception as e:  # noqa: BLE001
                doc, fail = None, common.clean_reason(e)

            if doc and not doc.get("bank_account"):
                fail = f"No bank account for {txn.get('currency')} on {target.bank_account}"

            if doc and not fail:
                twin = hand.match(doc["bank_account"], doc.get("date"), doc.get("deposit"), doc.get("withdrawal"))
                if twin:
                    item = _brief(txn, target.bank_account)
                    item["hand_line"] = twin
                    report["by_hand"].append(item)
                    part["by_hand"] = part.get("by_hand", 0) + 1
                    continue

            if dry_run and not fail:
                report["would_add"].append(_brief(txn, target.bank_account))
                continue
            if not fail:
                name, fail = common.insert_line(doc, f"awx_line_{n}")
                if name:
                    part["created"] += 1
                    report["created"] += 1
                    if part["created"] % common.COMMIT_EVERY == 0:
                        frappe.db.commit()
                    continue
            part["failed"] += 1
            item = _brief(txn, target.bank_account)
            item["reason"] = fail
            item["client"] = part["client"]
            report["failed"].append(item)

    return report


def _brief(txn, bank_account):
    amount = txn.get("net")
    if amount is None:
        amount = txn.get("amount")
    return {
        "id": txn.get("id"),
        "when": txn.get("created_at"),
        "currency": txn.get("currency"),
        "amount": amount,
        "type": txn.get("transaction_type"),
        "source": txn.get("source_type"),
        "description": (txn.get("description") or "")[:120],
        "bank_account": bank_account,
    }


def _finish(report, run, started, status):
    report["run"] = run
    report["started"] = str(started)
    report["ended"] = str(now_datetime())
    report["status"] = status
    common.save_report(DT, report)


def _status_of(report):
    return "Completed with Errors" if report["failed"] else "Completed"


def _log_summary(run, report):
    fails = len(report["failed"])
    words = (
        f"Airwallex {run}: {report['created']} added, {fails} failed, "
        f"window {report['window'][0]} to {report['window'][1]}"
    )
    bi_log.create_log(provider="Airwallex", message=words, status="Error" if fails else "Info")


# ------------------------------------------------------------------ scheduled

def sync_scheduled_transactions(setting_name=None, schedule_type=None, force=False):
    """The scheduled run: window from the last good run less the look-back.
    force: run now whatever the schedule (Sync now on the Bank Feeds page)."""
    settings = frappe.get_single(DT)
    if not settings.is_enabled():
        return
    if not force and settings.sync_schedule != schedule_type:
        return
    try:
        note = common.claim_lock(DT, "Airwallex")
    except common.FeedBusy as e:
        frappe.logger("bank_integration").info(str(e))
        return
    if note:
        bi_log.create_log(provider="Airwallex", message=note, status="Error")

    started = now_datetime()
    status = "Failed"
    try:
        first_days = {"Hourly": 1, "Daily": 2, "Weekly": 8, "Monthly": 31}.get(schedule_type, 1)
        from_dt = common.window_start(settings.get(CURSOR_F), first_days)
        report = run_window(settings, from_dt, started)
        status = _status_of(report)
        cursor = common.next_cursor(started, report["failed"], settings.get(CURSOR_F))
        common.set_single(DT, CURSOR_F, cursor)
        _finish(report, "sync now" if force else "scheduled", started, status)
        _backfill_after(started)
        if report["created"] or report["failed"]:
            _log_summary("scheduled run", report)
    except Exception:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "Airwallex scheduled sync failed")
        status = "Failed"
    finally:
        common.release_lock(DT, status)


# ------------------------------------------------------------------ manual / catch-up

def sync_transactions(from_date, to_date, setting_name=None, run="sync old", **kwargs):
    """A manual range (the "Sync old transactions" button, or catch-up).

    Takes the same lock as the scheduled run. Never moves the scheduled
    start point.
    """
    settings = frappe.get_single(DT)
    try:
        note = common.claim_lock(DT, "Airwallex")
    except common.FeedBusy as e:
        bi_log.create_log(provider="Airwallex", message=f"{run} not started: {e}", status="Error")
        return {"busy": str(e)}
    if note:
        bi_log.create_log(provider="Airwallex", message=note, status="Error")

    started = now_datetime()
    status = "Failed"
    report = None
    try:
        report = run_window(settings, get_datetime(from_date), get_datetime(to_date))
        status = _status_of(report)
        _finish(report, run, started, status)
        _backfill_after(get_datetime(to_date))
        _log_summary(run, report)
    except Exception:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), f"Airwallex {run} failed")
    finally:
        common.release_lock(DT, status)
    return report


def _backfill_after(to_dt):
    """Fill reference names that cardholders added late (unchanged rule)."""
    try:
        from frappe.utils import add_days, getdate

        from bank_integration.airwallex.backfill import backfill_reference_numbers

        bf_to = getdate(to_dt)
        backfill_reference_numbers(
            from_date=add_days(bf_to, -60).isoformat(), to_date=bf_to.isoformat()
        )
    except Exception as e:  # noqa: BLE001
        frappe.log_error(
            message=f"Reference number backfill after sync failed: {str(e)[:400]}",
            title="AirW Sync Backfill Error",
        )


def transaction_exists(transaction_id):
    """True when a Bank Transaction carries this transaction_id."""
    return frappe.db.exists("Bank Transaction", {"transaction_id": transaction_id})
