"""Kept so a job queued before the 17 Nov 26 change still runs. The
schedule now calls bank_integration.feed.scheduler."""

from bank_integration.airwallex.transaction import sync_scheduled_transactions


def run_hourly_sync():
    sync_scheduled_transactions(None, "Hourly")


def run_daily_sync():
    sync_scheduled_transactions(None, "Daily")


def run_weekly_sync():
    sync_scheduled_transactions(None, "Weekly")


def run_monthly_sync():
    sync_scheduled_transactions(None, "Monthly")
