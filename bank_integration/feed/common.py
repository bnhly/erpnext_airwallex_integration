"""Shared pieces for the bank feeds (Airwallex and Skript).

Rules every feed run follows (17 Nov 26, after lines went missing):

- A run asks for a WINDOW that starts LOOKBACK_DAYS before the last good
  run, so a line that arrives late, or failed last time, is asked for again.
  Lines already in ERPNext are skipped by transaction_id, so this is safe.
- Each line is inserted on its own savepoint. One bad line never takes the
  others with it, and never leaves half a document behind.
- A run with any failure ends "Completed with Errors" and does not move the
  start of the next window past the earliest failure (held at most
  MAX_HOLD_DAYS, so one line that can never go in cannot grow the window for
  ever; it stays in the run report until fixed).
- "In Progress" older than STALE_HOURS is a run that died. The next run
  takes over instead of waiting for ever.
"""

import json
from datetime import timedelta

import frappe
from frappe.utils import get_datetime, now_datetime

AIRWALLEX = "Airwallex Settings"
SKRIPT = "Skript Settings"
DOCTYPE = {"airwallex": AIRWALLEX, "skript": SKRIPT}

# Every provider settings record carries these run fields.
STATUS_F = "sync_status"
STARTED_F = "sync_started_on"
REPORT_F = "sync_report"
CURSOR_F = "last_sync_date"

LOOKBACK_DAYS = 14
MAX_HOLD_DAYS = 45
STALE_HOURS = 3
COMMIT_EVERY = 20
MAX_REPORT_FAILURES = 200


class FeedBusy(Exception):
    """Another run holds the lock."""


# ---------------------------------------------------------------- lock

def claim_lock(dt, who):
    """Mark a feed In Progress, unless a live run already holds it.

    Reads the two Singles rows FOR UPDATE so two workers cannot both claim.
    A run In Progress for more than STALE_HOURS (or with no start time) is
    treated as dead and taken over. Returns a note when it took one over.
    Raises FeedBusy when a live run holds it.
    """
    rows = frappe.db.sql(
        """select field, value from `tabSingles`
           where doctype=%s and field in (%s, %s) for update""",
        (dt, STATUS_F, STARTED_F),
    )
    vals = {r[0]: r[1] for r in rows}
    note = ""
    if vals.get(STATUS_F) == "In Progress":
        started = get_datetime(vals.get(STARTED_F)) if vals.get(STARTED_F) else None
        if started and now_datetime() - started < timedelta(hours=STALE_HOURS):
            raise FeedBusy(f"{who}: a run started at {started} is still going")
        note = (
            f"{who}: took over a run left In Progress since {started or 'an unknown time'}"
        )
    set_single(dt, STATUS_F, "In Progress")
    set_single(dt, STARTED_F, now_datetime())
    frappe.db.commit()
    return note


def release_lock(dt, status):
    set_single(dt, STATUS_F, status)
    frappe.db.commit()


# ---------------------------------------------------------------- window

def window_start(last_good, first_run_days, floor=None):
    """Start of the window to ask for: last good run less LOOKBACK_DAYS,
    never on or before the reconciled-up-to date (floor)."""
    now = now_datetime()
    if last_good:
        start = get_datetime(last_good) - timedelta(days=LOOKBACK_DAYS)
    else:
        start = now - timedelta(days=max(first_run_days, LOOKBACK_DAYS))
    if floor:
        start = max(start, get_datetime(floor) + timedelta(days=1))
    return start


SLICE_DAYS = 31


def slices(from_dt, to_dt):
    """The range in pieces of SLICE_DAYS, for the bank's list calls. Lines
    are judged once over the whole range, not per piece."""
    cur = get_datetime(from_dt)
    end = get_datetime(to_dt)
    while cur < end:
        nxt = min(cur + timedelta(days=SLICE_DAYS), end)
        yield cur, nxt
        cur = nxt


def next_cursor(run_started, failures, old_cursor):
    """Where the next window is measured from.

    Clean run: the moment this run started (its own window ended there).
    Failures: no later than the earliest failed line, held no further back
    than MAX_HOLD_DAYS.
    """
    if not failures:
        return run_started
    times = [t for t in (site_time(f.get("when")) for f in failures) if t]
    earliest = min(times) if times else run_started
    floor = run_started - timedelta(days=MAX_HOLD_DAYS)
    hold = max(earliest, floor)
    if old_cursor:
        hold = min(hold, max(get_datetime(old_cursor), floor))
    return min(hold, run_started)


def site_time(value):
    """A naive datetime in the site's time zone, from anything (ISO with an
    offset, naive, datetime). None when it cannot be read."""
    if not value:
        return None
    try:
        dt = value if hasattr(value, "tzinfo") else get_datetime(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt is None:
        return None
    if getattr(dt, "tzinfo", None) is not None:
        import pytz

        dt = dt.astimezone(pytz.timezone(frappe.utils.get_system_timezone())).replace(tzinfo=None)
    return dt


# ---------------------------------------------------------------- lines

def existing_ids(ids):
    """transaction_id values already on a Bank Transaction (any docstatus).

    A cancelled line counts as existing: someone removed it on purpose.
    """
    ids = [i for i in ids if i]
    found = set()
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        for (tid,) in frappe.db.sql(
            "select transaction_id from `tabBank Transaction` where transaction_id in %s",
            (tuple(chunk),),
        ):
            found.add(tid)
    return found


NEAR_DAYS = 3
NEW, THERE, UNSURE = "NEW", "ALREADY THERE", "UNSURE"


class Matcher:
    """Decides whether a bank line the feed has (and ERPNext has no line with
    its transaction_id) is really missing.

    A feed line is ALREADY THERE when a live Bank Transaction (draft or
    submitted, not cancelled) of the same amount and direction sits within
    NEAR_DAYS of it, on its Bank Account or on no Bank Account in the same
    currency, whatever that line's transaction_id (a statement import, a hand
    entry, a line the feed made under another id, the C7 rows). Lines that
    carry an id fetched in this run belong to their own feed line and are
    not offered. Each existing line is claimed once, nearest date first,
    feed lines taken in date order.

    Verdicts: NEW (no candidate), ALREADY THERE (a line was claimed and no
    spare candidate is left over), UNSURE (claimed, but another candidate
    was left unclaimed, so it could be either).

    Rules from the FY26 reconciliation (17 Nov 26); the read-only check
    accounts/tm_fy26_awx_catchup_check.py in erp_client_server sorts lines
    the same way.
    """

    def __init__(self, own_ids=()):
        self.own = {i for i in own_ids if i}
        self.items = []

    def add(self, key, bank_account, currency, day, deposit, withdrawal):
        from frappe.utils import flt, getdate

        self.items.append({
            "key": key, "bank_account": bank_account, "currency": currency,
            "day": getdate(day), "deposit": round(flt(deposit), 2), "withdrawal": round(flt(withdrawal), 2),
        })

    def _candidates(self, it):
        from frappe.utils import add_days

        col = "deposit" if it["deposit"] else "withdrawal"
        amt = it[col]
        rows = frappe.db.sql(
            f"""select name, date, docstatus, status, ifnull(transaction_id, '') tid,
                       ifnull(bank_account, '') bank_account, description,
                       ifnull(allocated_amount, 0) allocated
                from `tabBank Transaction`
                where docstatus < 2 and {col} = %(amt)s
                  and ifnull({'withdrawal' if col == 'deposit' else 'deposit'}, 0) = 0
                  and (bank_account = %(ba)s
                       or (ifnull(bank_account, '') = '' and currency = %(cur)s))
                  and date between %(d0)s and %(d1)s""",
            {"amt": amt, "ba": it["bank_account"], "cur": it["currency"],
             "d0": add_days(it["day"], -NEAR_DAYS), "d1": add_days(it["day"], NEAR_DAYS)},
            as_dict=True,
        )
        rows = [r for r in rows if not (r.tid and r.tid in self.own)]
        rows.sort(key=lambda r: (abs((r.date - it["day"]).days), r.docstatus, r.name))
        return rows

    def judge(self):
        """{key: {verdict, line, line_info, candidates, adopt, why}}"""
        claimed = set()
        out = {}
        order = sorted(self.items, key=lambda i: (i["day"], i["key"] or ""))
        for it in order:
            cands = self._candidates(it)
            free = [c for c in cands if c.name not in claimed]
            res = {"candidates": [c.name for c in free], "line": None}
            if free:
                pick = free[0]
                claimed.add(pick.name)
                res["line"] = pick.name
                res["line_info"] = {
                    "date": str(pick.date), "bank_account": pick.bank_account or None,
                    "transaction_id": pick.tid or None, "status": ("Draft " if pick.docstatus == 0 else "") + str(pick.status or ""),
                    "description": (pick.description or "")[:60], "matched": bool(pick.allocated),
                }
            out[it["key"]] = res
        for it in order:
            res = out[it["key"]]
            if not res["line"]:
                res["verdict"] = NEW
                continue
            spare = [c for c in res["candidates"] if c != res["line"] and c not in claimed]
            res["verdict"] = UNSURE if spare else THERE
            info = res["line_info"]
            why = None
            if res["verdict"] != THERE or len(res["candidates"]) != 1:
                why = "more than one line could be it"
            elif not info["bank_account"]:
                why = "the line has no Bank Account (ask before setting one)"
            elif info["transaction_id"]:
                why = "the line already carries another id"
            elif info["matched"]:
                why = "the line is matched in Bank Reconciliation"
            res["adopt"] = why is None
            res["why"] = why
        return out


def adopt_line(line, transaction_id):
    """Write the feed's id onto an existing line so later runs skip it.

    Nothing else changes: not the amount, date, status, Bank Account or any
    match. Refused when the line moved since it was judged."""
    row = frappe.db.get_value(
        "Bank Transaction", line,
        ["docstatus", "transaction_id", "bank_account", "allocated_amount"], as_dict=True, for_update=True,
    )
    if not row or row.docstatus == 2:
        return "the line is gone or cancelled"
    if row.transaction_id:
        return "the line already carries an id"
    if not row.bank_account:
        return "the line has no Bank Account"
    if (row.allocated_amount or 0) > 0:
        return "the line is matched in Bank Reconciliation"
    if frappe.db.exists("Bank Transaction", {"transaction_id": transaction_id}):
        return "another line already carries this id"
    frappe.db.set_value("Bank Transaction", line, "transaction_id", transaction_id, update_modified=False)
    return None


def insert_line(doc_dict, sp_name):
    """Insert and submit one Bank Transaction on its own savepoint.

    Returns (name, None) or (None, reason). On failure the savepoint is
    rolled back, so nothing of the line is left behind.
    """
    frappe.db.savepoint(sp_name)
    try:
        doc = frappe.get_doc(doc_dict)
        doc.insert()
        doc.submit()
        frappe.db.release_savepoint(sp_name)
        return doc.name, None
    except Exception as e:  # noqa: BLE001 - every line failure is reported, not raised
        frappe.db.rollback(save_point=sp_name)
        frappe.clear_messages()
        return None, clean_reason(e)


def clean_reason(e):
    if isinstance(e, frappe.MandatoryError):
        # Frappe says "[Bank Transaction, ACC-BTN-..]: reference_number"
        fields = str(e).rsplit("]:", 1)[-1].strip()
        meta = frappe.get_meta("Bank Transaction")
        labels = [meta.get_label(f.strip()) or f.strip() for f in fields.split(",") if f.strip()]
        return "Missing: " + ", ".join(labels)
    text = str(e) or e.__class__.__name__
    text = frappe.utils.strip_html(text) if "<" in text else text
    return " ".join(text.split())[:300]


# ---------------------------------------------------------------- report

def save_report(dt, report):
    """Keep the last run's report (JSON) on the settings record."""
    fails = report.get("failed") or []
    if len(fails) > MAX_REPORT_FAILURES:
        report["failed_more"] = len(fails) - MAX_REPORT_FAILURES
        report["failed"] = fails[:MAX_REPORT_FAILURES]
    set_single(dt, REPORT_F, json.dumps(report, default=str, indent=1))


def set_single(dt, field, value):
    """Write one settings value without touching modified (a form open on
    the page would otherwise say the record changed under it)."""
    frappe.db.set_single_value(dt, field, value, update_modified=False)


def report_of(dt):
    raw = frappe.db.get_single_value(dt, REPORT_F)
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None
