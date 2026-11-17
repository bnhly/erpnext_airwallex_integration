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

def window_start(last_good, first_run_days):
    """Start of the window to ask for: last good run less LOOKBACK_DAYS."""
    now = now_datetime()
    if last_good:
        return get_datetime(last_good) - timedelta(days=LOOKBACK_DAYS)
    return now - timedelta(days=max(first_run_days, LOOKBACK_DAYS))


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


HAND_DAYS = 3


class HandLines:
    """Bank lines made by hand (no transaction_id): statement imports and the
    register's scripts. A feed line with the same bank account, amount and
    direction within HAND_DAYS of one of them is taken to be that line and is
    NOT added again; it is listed in the run report instead. Each hand line
    stands for one feed line only. Cancelled hand lines do not count.

    Added 17 Nov 26: the look-back window and catch-up would otherwise bring
    in a second copy of lines that were loaded by hand while the feed was
    broken (CBA credit card lines F6, the Airwallex fee C6).
    """

    def __init__(self):
        self.pool = {}
        self.taken = set()

    def _load(self, bank_account, day):
        from frappe.utils import add_days, getdate

        key = (bank_account, str(getdate(day))[:7])
        if key in self.pool:
            return self.pool[key]
        d = getdate(day)
        rows = frappe.db.sql(
            """select name, date, deposit, withdrawal from `tabBank Transaction`
               where bank_account=%s and docstatus<2 and ifnull(transaction_id,'')=''
                 and date between %s and %s""",
            (bank_account, add_days(d.replace(day=1), -HAND_DAYS - 1),
             add_days(d.replace(day=28), HAND_DAYS + 5)),
            as_dict=True,
        )
        self.pool[key] = rows
        return rows

    def match(self, bank_account, day, deposit, withdrawal):
        """Name of the hand line this feed line duplicates, or None."""
        from frappe.utils import date_diff, flt, getdate

        if not (bank_account and day):
            return None
        dep, wd = round(flt(deposit), 2), round(flt(withdrawal), 2)
        best = None
        for r in self._load(bank_account, day):
            if r.name in self.taken:
                continue
            if round(flt(r.deposit), 2) != dep or round(flt(r.withdrawal), 2) != wd:
                continue
            gap = abs(date_diff(getdate(r.date), getdate(day)))
            if gap <= HAND_DAYS and (best is None or gap < best[0]):
                best = (gap, r.name)
        if best:
            self.taken.add(best[1])
            return best[1]
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
