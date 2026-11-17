"""Scheduled Airwallex runs. Each one runs only when it matches the chosen
schedule. Whether another run is still going (or died and left the lock)
is decided inside sync_scheduled_transactions, not here: the old check here
skipped every run for good once a run died "In Progress"."""

import frappe

from bank_integration.airwallex.transaction import sync_scheduled_transactions


def _run(schedule_type):
    try:
        setting = frappe.get_single("Bank Integration Setting")
        if setting.enable_airwallex and setting.sync_schedule == schedule_type:
            sync_scheduled_transactions("Bank Integration Setting", schedule_type)
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"Airwallex {schedule_type} sync error")


def run_hourly_sync():
    _run("Hourly")


def run_daily_sync():
    _run("Daily")


def run_weekly_sync():
    _run("Weekly")


def run_monthly_sync():
    _run("Monthly")
