"""Clearing out Bank Integration Log (and the feed's Error Log noise).

Bank Integration Log held 65,000+ rows by 12 Nov 26: until 17 Nov 26 every
HTTP call to Airwallex and Skript was written there whatever the Enable Log
tick said, with the request headers unmasked (the Airwallex login rows carry
the long-life x-api-key, the others a bearer token) and the full reply
(bank data). The fix stops new ones; this clears the old.

purge_logs is a DRY RUN unless dry_run=0 AND confirm=YES_DELETE. The delete
runs as a background job in batches of BATCH rows, committing after each,
so it never holds a long lock and can be stopped and run again.

  /api/method/bank_integration.feed.maintenance.purge_logs
      ?before=2026-11-18                       counts only (default: now)
      ?before=2026-11-18&error_logs=1          also the feed's Error Log rows
      ?before=2026-11-18&dry_run=0&confirm=YES_DELETE      delete
"""

import frappe
from frappe.utils import get_datetime, now_datetime

BATCH = 2000
LOG = "Bank Integration Log"

# Error Log titles the feed writes (method column). Only these are touched.
ERROR_TITLES = (
    "Txn Error - %",
    "Sync Error - %",
    "API Error - %",
    "Auth-%",
    "Auth Failed - %",
    "Token-Invalid-%",
    "Token Cache Error",
    "Token DB Error",
    "Client Doc Error",
    "Skript Transaction Error",
    "Skript Scheduled Sync Error",
    "Skript Sync Error",
    "Skript Token%",
    "Skript Log Error",
    "Bank Integration Log Creation Error",
    "Hourly Sync Error",
    "Daily Sync Error",
    "Scheduled Sync Error - %",
)

SECRET_LIKE = ("%x-api-key%", "%Bearer %", "%'Authorization'%", "%\"token\"%", "%'token'%")


def _allowed():
    if frappe.session.user == "Administrator" or "System Manager" in frappe.get_roles():
        return
    frappe.throw("Only a System Manager can clear bank feed logs.", frappe.PermissionError)


def _error_where():
    clause = " or ".join(["method like %s"] * len(ERROR_TITLES))
    return f"({clause})", list(ERROR_TITLES)


def counts(before):
    before = get_datetime(before)
    total = frappe.db.count(LOG)
    old = frappe.db.sql(f"select count(*) from `tab{LOG}` where creation < %s", (before,))[0][0]
    secret_clause = " or ".join(["request_headers like %s"] * len(SECRET_LIKE)) + " or " + " or ".join(
        ["response_data like %s"] * len(SECRET_LIKE)
    )
    secret = frappe.db.sql(
        f"select count(*) from `tab{LOG}` where {secret_clause}", tuple(SECRET_LIKE) * 2
    )[0][0]
    span = frappe.db.sql(f"select min(creation), max(creation) from `tab{LOG}`")[0]
    where, args = _error_where()
    err = frappe.db.sql(
        f"select count(*) from `tabError Log` where creation < %s and {where}", [before] + args
    )[0][0]
    by_title = frappe.db.sql(
        f"""select method, count(*) n from `tabError Log` where {where}
            group by method order by n desc limit 20""",
        args,
        as_dict=True,
    )
    return {
        "before": str(before),
        "bank_integration_log": {
            "total": total,
            "to_delete": old,
            "holding_keys_or_tokens": secret,
            "oldest": str(span[0]) if span[0] else None,
            "newest": str(span[1]) if span[1] else None,
        },
        "error_log_feed_rows_to_delete": err,
        "error_log_feed_titles": by_title,
    }


@frappe.whitelist()
def purge_logs(before=None, dry_run=1, confirm=None, error_logs=0):
    _allowed()
    before = get_datetime(before) if before else now_datetime()
    out = counts(before)
    out["dry_run"] = str(dry_run) != "0"
    out["error_logs"] = str(error_logs) == "1"
    if out["dry_run"]:
        out["next"] = "Add &dry_run=0&confirm=YES_DELETE to delete these rows."
        return out
    if confirm != "YES_DELETE":
        frappe.throw("To delete, add &confirm=YES_DELETE as well as &dry_run=0.")
    frappe.enqueue(
        "bank_integration.feed.maintenance.purge_job",
        queue="long",
        timeout=3600,
        job_id="bank_feed_log_purge",
        deduplicate=True,
        before=str(before),
        error_logs=out["error_logs"],
        by=frappe.session.user,
    )
    out["started"] = "Deleting in the background. Run the dry run again to see what is left."
    return out


def delete_logs_before(before, batch=BATCH, max_batches=None):
    """Delete Bank Integration Log rows made before `before`, in batches."""
    gone = 0
    n = 0
    while True:
        names = frappe.db.sql_list(
            f"select name from `tab{LOG}` where creation < %s limit %s", (get_datetime(before), batch)
        )
        if not names:
            break
        frappe.db.delete(LOG, {"name": ("in", names)})
        frappe.db.commit()
        gone += len(names)
        n += 1
        if max_batches and n >= max_batches:
            break
    return gone


def delete_error_logs_before(before, batch=BATCH):
    where, args = _error_where()
    gone = 0
    while True:
        names = frappe.db.sql_list(
            f"select name from `tabError Log` where creation < %s and {where} limit %s",
            [get_datetime(before)] + args + [batch],
        )
        if not names:
            break
        frappe.db.delete("Error Log", {"name": ("in", names)})
        frappe.db.commit()
        gone += len(names)
    return gone


def purge_job(before, error_logs=False, by=None):
    logs = delete_logs_before(before)
    errs = delete_error_logs_before(before) if error_logs else 0
    from bank_integration.bank_integration.doctype.bank_integration_log import bank_integration_log as bi_log

    bi_log.create_log(
        f"Cleared {logs} Bank Integration Log rows and {errs} feed Error Log rows "
        f"made before {before} (asked by {by or 'unknown'})",
        status="Info",
    )
    frappe.db.commit()
