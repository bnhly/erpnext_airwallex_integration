import frappe
from bank_integration.airwallex.api.financial_transactions import FinancialTransactions
from bank_integration.airwallex.api.base_api import AirwallexAPIError  # Add this import
from bank_integration.airwallex.utils import map_airwallex_to_erpnext, build_expense_merchant_index
from bank_integration.bank_integration.doctype.bank_integration_log import bank_integration_log as bi_log
from datetime import datetime
import traceback


def sync_transactions(from_date, to_date, setting_name):
    """Sync transactions for all configured clients"""
    settings = frappe.get_doc("Bank Integration Setting", setting_name)

    if not settings.airwallex_clients:
        frappe.throw("No Airwallex clients configured")

    total_processed = 0
    total_created = 0

    # Convert datetime to ISO8601 format if needed
    if hasattr(settings, '_to_iso8601'):
        from_date_iso = settings._to_iso8601(from_date)
        to_date_iso = settings._to_iso8601(to_date)
    else:
        # Fallback conversion
        from frappe.utils import get_datetime
        from_dt = get_datetime(from_date) if from_date else None
        to_dt = get_datetime(to_date) if to_date else None
        from_date_iso = from_dt.strftime('%Y-%m-%dT%H:%M:%SZ') if from_dt else None
        to_date_iso = to_dt.strftime('%Y-%m-%dT%H:%M:%SZ') if to_dt else None

    for client in settings.airwallex_clients:
        try:
            # Sync transactions for this specific client
            processed, created = sync_client_transactions(
                client, from_date_iso, to_date_iso, settings
            )
            total_processed += processed
            total_created += created

        except Exception as e:
            # Shorten the error message for the log title
            client_short = client.airwallex_client_id[:8] if client.airwallex_client_id else "unknown"
            error_title = f"Sync Error - Client {client_short}"

            # Create detailed error message (truncated to avoid length issues)
            error_message = f"Failed to sync transactions for client {client.airwallex_client_id}: {str(e)[:500]}"

            frappe.log_error(message=error_message, title=error_title)

            # Also log to Bank Integration Log
            try:
                bi_log.create_log(
                    f"Sync failed for client {client.airwallex_client_id}: {str(e)[:200]}",
                    status="Error"
                )
            except Exception as log_error:
                frappe.logger().error(f"Failed to create integration log: {str(log_error)}")

    # After every sync, sweep recent Bank Transactions for any that
    # still have an empty reference_number. Cardholders frequently
    # review and fill in expense descriptions in Airwallex days after
    # the underlying card swipe, so the row created by the original
    # sync misses the data. A wider rolling window than the sync window
    # catches those late edits.
    try:
        from bank_integration.airwallex.backfill import backfill_reference_numbers
        from frappe.utils import getdate, add_days, today
        bf_to = getdate(to_date or today())
        bf_from = add_days(bf_to, -60)
        backfill_reference_numbers(
            from_date=bf_from.isoformat(),
            to_date=bf_to.isoformat(),
        )
    except Exception as e:
        frappe.log_error(
            message=f"Reference number backfill after sync failed: {str(e)[:400]}",
            title="AirW Sync Backfill Error",
        )

    # Update final status and last sync date
    settings.update_sync_progress(total_processed, total_processed, "Completed")
    # Update last sync date to current time for successful completion
    settings.db_set('last_sync_date', frappe.utils.now())


def sync_client_transactions(client, from_date_iso, to_date_iso, settings):
    """Sync transactions for a specific client"""
    try:
        # Initialize FinancialTransactions with proper credentials
        api = FinancialTransactions(
            client_id=client.airwallex_client_id,
            api_key=client.get_password("airwallex_api_key"),
            api_url=settings.api_url
        )

        # The API will automatically authenticate when needed
        # Pass ISO8601 formatted dates to the API
        transactions = api.get_list(from_created_at=from_date_iso, to_created_at=to_date_iso)
        processed = 0
        created = 0
        skipped = 0

        if not transactions:
            return 0, 0

        # Handle different response formats
        if isinstance(transactions, dict):
            # If the response is paginated or wrapped
            transactions = transactions.get('items', transactions.get('data', []))

        # Pull the Spend Expenses for the same window once, so we can match
        # CARD_PURCHASE / CARD_REFUND rows to their merchant name without
        # an API call per row. The Issuing Transactions API would be the
        # cleaner source but it is gated on this client_id.
        expense_index = build_expense_merchant_index(
            client, settings.api_url, from_date_iso, to_date_iso
        )
        # Shared across the per-transaction loop so each Issuing
        # transaction (the source of the clean merchant name on
        # CARD_PURCHASE rows) is fetched at most once per sync.
        issuing_cache = {}

        for txn in transactions:
            try:
                transaction_id = txn.get('id')
                transaction_type = txn.get('transaction_type', '').upper()
                transaction_currency = txn.get('currency')

                # Check if transaction already exists
                if transaction_exists(transaction_id):
                    frappe.logger().info(f"Transaction {transaction_id} already exists, skipping")
                    processed += 1
                    skipped += 1
                    continue

                # Check transaction type filtering
                if not settings.should_sync_transaction(transaction_type):
                    frappe.logger().info(f"Transaction {transaction_id} type '{transaction_type}' filtered out, skipping")
                    processed += 1
                    skipped += 1
                    continue

                # Check if transaction has currency (basic validation)
                if not transaction_currency:
                    frappe.logger().warning(f"Transaction {transaction_id} has no currency, skipping")
                    processed += 1
                    skipped += 1
                    continue

                # Map transaction to client's bank account
                bank_txn = map_airwallex_to_erpnext(
                    txn,
                    client.bank_account,
                    client=client,
                    api_url=settings.api_url,
                    expense_index=expense_index,
                    issuing_cache=issuing_cache,
                )
                bank_txn_doc = frappe.get_doc(bank_txn)
                bank_txn_doc.insert()
                bank_txn_doc.submit()
                created += 1

                frappe.logger().info(f"Created transaction {transaction_id} of type {transaction_type}")

                processed += 1

                # Update progress periodically (every 10 transactions)
                if processed % 10 == 0:
                    settings.update_sync_progress(processed, len(transactions))

            except Exception as txn_error:
                client_short = client.airwallex_client_id[:8]
                frappe.log_error(
                    message=f"Failed to process transaction {txn.get('id', 'unknown')}: {str(txn_error)[:300]}",
                    title=f"Txn Error - {client_short}"
                )

        # Final progress update
        if hasattr(settings, 'update_sync_progress'):
            settings.update_sync_progress(processed, len(transactions))

        # Log summary
        frappe.logger().info(f"Client {client.airwallex_client_id[:8]}: Processed {processed}, Created {created}, Skipped {skipped}")

        return processed, created

    except AirwallexAPIError as e:
        client_short = client.airwallex_client_id[:8] if client.airwallex_client_id else "unknown"
        frappe.log_error(
            message=f"API Error for client {client.airwallex_client_id}: {str(e.message)[:300]}",
            title=f"API Error - {client_short}"
        )
        return 0, 0

    except Exception as e:
        client_short = client.airwallex_client_id[:8] if client.airwallex_client_id else "unknown"
        frappe.log_error(
            message=f"Sync failed for client {client.airwallex_client_id}: {str(e)[:300]}",
            title=f"Sync Error - {client_short}"
        )
        return 0, 0


def transaction_exists(transaction_id):
    """
    Check if a Bank Transaction with the given transaction ID already exists
    """
    # Check using the transaction_id field
    return frappe.db.exists("Bank Transaction", {"transaction_id": transaction_id})


def sync_scheduled_transactions(setting_name, schedule_type):
    """
    Sync transactions based on schedule type
    """
    from datetime import datetime, timedelta

    try:
        # For single doctype, use get_single instead of get_doc
        setting = frappe.get_single("Bank Integration Setting")

        # Check if sync is already in progress
        if setting.sync_status == "In Progress":
            frappe.logger().info(f"Sync already in progress, skipping {schedule_type} sync")
            return

        if not setting.is_enabled():
            frappe.logger().info(f"Airwallex integration disabled")
            return

        # Set status to prevent concurrent runs
        setting.db_set('sync_status', 'In Progress')

        # Calculate date range based on schedule type
        end_date = frappe.utils.now_datetime()

        # Use last sync date as start date if available, otherwise use schedule-based calculation
        if setting.last_sync_date:
            start_date = frappe.utils.get_datetime(setting.last_sync_date)
            frappe.logger().info(f"Using last sync date as start: {start_date}")
        else:
            # Fallback to schedule-based calculation for first run
            if schedule_type == "Hourly":
                # Sync last 2 hours
                start_date = end_date - timedelta(hours=2)
            elif schedule_type == "Daily":
                # Sync yesterday
                start_date = end_date - timedelta(days=1)
            elif schedule_type == "Weekly":
                # Sync last 7 days
                start_date = end_date - timedelta(days=7)
            elif schedule_type == "Monthly":
                # Sync last 30 days
                start_date = end_date - timedelta(days=30)
            else:
                frappe.logger().error(f"Unknown schedule type: {schedule_type}")
                setting.db_set('sync_status', 'Failed')
                return

        bi_log.create_log(f"Starting scheduled {schedule_type} sync from {start_date} to {end_date}")
        frappe.logger().info(f"Starting scheduled {schedule_type} sync from {start_date} to {end_date}")

        # Use the existing sync function with calculated dates
        # Pass the doctype name since it's a single doctype
        sync_transactions(start_date, end_date, "Bank Integration Setting")

        # Update last sync date on successful completion
        setting.db_set('last_sync_date', frappe.utils.now())
        frappe.logger().info(f"Scheduled {schedule_type} sync completed successfully")

    except Exception as e:
        # Make sure to reset status on error
        try:
            setting = frappe.get_single("Bank Integration Setting")
            setting.db_set('sync_status', 'Failed')
        except:
            pass

        error_msg = f"Scheduled {schedule_type} sync failed: {str(e)}"
        bi_log.create_log(error_msg, status="Error")
        frappe.log_error(frappe.get_traceback(), f"Scheduled Sync Error - {schedule_type}")
        frappe.logger().error(error_msg)
