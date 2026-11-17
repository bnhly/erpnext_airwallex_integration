"""One entry point per schedule. Each provider runs only when its own
"Sync Every" matches; whether a run is still going, or died and left the
lock, is decided inside the provider's run (feed/common.py)."""

import frappe

PROVIDERS = (
    ("Airwallex", "bank_integration.airwallex.transaction.sync_scheduled_transactions"),
    ("Skript", "bank_integration.skript.skript_transaction.sync_scheduled_transactions_skript"),
)


def _run(schedule_type):
    for name, path in PROVIDERS:
        try:
            frappe.get_attr(path)(None, schedule_type)
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"{name} {schedule_type} sync error")


def hourly():
    _run("Hourly")


def daily():
    _run("Daily")


def weekly():
    _run("Weekly")


def monthly():
    _run("Monthly")
