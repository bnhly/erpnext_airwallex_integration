"""Scheduled Skript runs. Whether another run is going, or died and left
the lock, is decided inside sync_scheduled_transactions_skript."""

import frappe

from bank_integration.skript.skript_transaction import sync_scheduled_transactions_skript


def _run(schedule_type):
    try:
        sync_scheduled_transactions_skript("Bank Integration Setting", schedule_type)
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"Skript {schedule_type} sync error")


def run_hourly_skript_sync():
    _run("Hourly")


def run_daily_skript_sync():
    _run("Daily")


def run_weekly_skript_sync():
    _run("Weekly")


def run_monthly_skript_sync():
    _run("Monthly")


def complete_skript_sync():
    """Retired 17 Nov 26 (taken off the 23:00 schedule in hooks.py).

    It marked Scheduled Job Log rows Complete and forced the Skript status
    to Completed every night, whatever was running. The lock now times out
    on its own (feed/common.py). Kept so an old queued call does not fail.
    """
    return
