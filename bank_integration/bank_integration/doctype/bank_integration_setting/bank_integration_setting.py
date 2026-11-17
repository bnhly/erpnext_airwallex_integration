# Copyright (c) 2025, Akhilam Inc and contributors
# For license information, please see license.txt

from bank_integration.airwallex.api.airwallex_authenticator import AirwallexAuthenticator
import frappe
from frappe.model.document import Document
from frappe.utils.background_jobs import enqueue
from frappe.utils import add_days, add_months, get_datetime, now_datetime
from frappe.utils.scheduler import is_scheduler_inactive
from datetime import datetime

class BankIntegrationSetting(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from bank_integration.bank_integration.doctype.airwallex_client.airwallex_client import AirwallexClient
        from bank_integration.bank_integration.doctype.skript_account.skript_account import SkriptAccount
        from bank_integration.bank_integration.doctype.transaction_type_filter.transaction_type_filter import TransactionTypeFilter
        from frappe.types import DF

        airwallex_clients: DF.Table[AirwallexClient]
        api_url: DF.Data | None
        enable_airwallex: DF.Check
        enable_log: DF.Check
        enable_skript: DF.Check
        file_url: DF.Data | None
        from_date: DF.Datetime | None
        last_sync_date: DF.Datetime | None
        sync_started_on: DF.Datetime | None
        sync_report: DF.LongText | None
        skript_sync_started_on: DF.Datetime | None
        skript_sync_report: DF.LongText | None
        processed_records: DF.Int
        skript_access_token: DF.SmallText | None
        skript_access_token_url: DF.Data | None
        skript_accounts: DF.Table[SkriptAccount]
        skript_api_scope: DF.Data | None
        skript_api_url: DF.Data | None
        skript_client_id: DF.Password | None
        skript_client_secret: DF.Password | None
        skript_consumer_id: DF.Data | None
        skript_from_date: DF.Datetime | None
        skript_last_sync_date: DF.Datetime | None
        skript_processed_records: DF.Int
        skript_sync_old_transactions: DF.Check
        skript_sync_progress: DF.Percent
        skript_sync_schedule: DF.Literal["Hourly", "Daily", "Weekly", "Monthly"]
        skript_sync_status: DF.Literal["Not Started", "In Progress", "Completed", "Completed with Errors", "Failed"]
        skript_to_date: DF.Datetime | None
        skript_token_expiry: DF.Datetime | None
        skript_total_records: DF.Int
        sync_old_transactions: DF.Check
        sync_progress: DF.Percent
        sync_schedule: DF.Literal["Hourly", "Daily", "Weekly", "Monthly"]
        sync_status: DF.Literal["Not Started", "In Progress", "Completed", "Completed with Errors", "Failed"]
        to_date: DF.Datetime | None
        total_records: DF.Int
        transaction_type_filters: DF.Table[TransactionTypeFilter]
    # end: auto-generated types

    def should_sync_transaction(self, transaction_type):
        """
        Check if a transaction type should be synced based on configured filters

        Args:
            transaction_type (str): The transaction type from Airwallex

        Returns:
            bool: True if transaction should be synced, False otherwise
        """
        # If no filters are configured, sync all transactions
        if not self.transaction_type_filters:
            return True

        # Check if transaction type matches any filter
        for filter_rule in self.transaction_type_filters:
            if filter_rule.transaction_type == transaction_type:
                # If it's an include filter, sync the transaction
                if filter_rule.filter_action == "Include":
                    return True
                # If it's an exclude filter, don't sync the transaction
                elif filter_rule.filter_action == "Exclude":
                    return False

        # If transaction type is not found in filters, check filter strategy
        # If there are any "Include" filters, default to exclude (whitelist approach)
        # If there are only "Exclude" filters, default to include (blacklist approach)
        has_include_filters = any(f.filter_action == "Include" for f in self.transaction_type_filters)

        if has_include_filters:
            # Whitelist approach: only included types are synced
            return False
        else:
            # Blacklist approach: all types except excluded ones are synced
            return True

    def is_enabled(self):
        return bool(self.enable_airwallex)

    def _to_iso8601(self, dt):
        """Convert datetime to ISO8601 format in UTC timezone"""
        try:
            from frappe.utils import get_datetime
            import pytz

            if not dt:
                return None

            # Convert to datetime object if it's a string
            if isinstance(dt, str):
                dt = get_datetime(dt)

            # If datetime is naive (no timezone info), assume it's in system timezone
            if dt.tzinfo is None:
                # Get system timezone from Frappe settings
                system_tz = pytz.timezone(frappe.utils.get_system_timezone())
                dt = system_tz.localize(dt)

            # Convert to UTC
            utc_dt = dt.astimezone(pytz.UTC)

            # Format as ISO8601 with 'Z' suffix for UTC
            return utc_dt.strftime('%Y-%m-%dT%H:%M:%SZ')

        except Exception as e:
            frappe.log_error(f"Error converting datetime to ISO8601: {str(e)}", "ISO8601 Conversion Error")
            return None

    def _credentials_changed(self):
        """Check if any client credentials have changed"""
        if self.is_new():
            return True

        # Get the old document
        old_doc = self.get_doc_before_save()
        if not old_doc:
            return True

        # Check if API URL changed
        if self.api_url != old_doc.api_url:
            return True

        # Check if client credentials changed
        old_clients = {client.airwallex_client_id: client for client in old_doc.airwallex_clients}
        current_clients = {client.airwallex_client_id: client for client in self.airwallex_clients}

        # Check for new or removed clients
        if set(old_clients.keys()) != set(current_clients.keys()):
            return True

        # Check if any client credentials changed
        for client_id, client in current_clients.items():
            old_client = old_clients.get(client_id)
            if old_client:
                # Check if API key changed (comparing raw values)
                if (client.get_password("airwallex_api_key") !=
                    old_client.get_password("airwallex_api_key")):
                    return True
                # Check if bank account changed
                if client.bank_account != old_client.bank_account:
                    return True

        return False

    def validate(self):
        """Validate settings - only test authentication when credentials change"""
        if self.enable_airwallex:
            # Only test authentication if this is a new document or credentials have changed
            if self._credentials_changed():
                # Test authentication and disable if it fails
                authentication_successful = self.test_authentication_silent()
                if not authentication_successful:
                    self.enable_airwallex = 0
                    frappe.msgprint(
                        "❌ Authentication failed for one or more clients. Airwallex integration has been disabled.",
                        indicator="red",
                        alert=True
                    )
        else:
            # If disabled, reset sync status
            self.sync_status = "Not Started"
            self.processed_records = 0
            self.total_records = 0
            self.sync_progress = 0
            # last_sync_date is kept: turning the feed off and on again must
            # not lose the lines from while it was off.

        # Skript validation
        if self.enable_skript:

            if self._skript_credentials_changed():
                # Test authentication and disable if it fails
                authentication_successful = self.test_skript_authentication_silent()
                if not authentication_successful:
                    self.enable_skript = 0
                    frappe.msgprint(
                        "❌ Skript authentication failed. Skript integration has been disabled. Please check your credentials.",
                        indicator="red",
                        alert=True
                    )

            # Update is_mapped flag for all rows
            for row in self.skript_accounts:
                row.is_mapped = 1 if row.bank_account else 0

    def on_update(self):
        """Trigger sync job when sync_old_transactions is enabled"""
        if self.enable_airwallex and self.sync_old_transactions and self.sync_status == "Not Started":
            self.start_transaction_sync()

    # Add method to get client configurations
    def get_airwallex_clients(self):
        """Get all configured Airwallex clients"""
        return [client for client in self.airwallex_clients if client.airwallex_client_id and client.bank_account]

    def test_authentication_silent(self):
        """Test authentication without showing messages - returns True/False"""
        if not self.airwallex_clients:
            return False

        success_count = 0
        total_clients = len(self.airwallex_clients)

        for client in self.airwallex_clients:
            try:
                api = AirwallexAuthenticator(
                    client_id=client.airwallex_client_id,
                    api_key=client.get_password("airwallex_api_key"),
                    api_url=self.api_url
                )
                response = api.authenticate()

                if response and response.get('token'):
                    success_count += 1

            except Exception as e:
                # Log the error but don't show message - use short title
                client_short = client.airwallex_client_id[:6] if client.airwallex_client_id else "unknown"
                frappe.log_error(
                    f"Authentication failed for client {client.airwallex_client_id}: {str(e)}",
                    f"Auth-Test-{client_short}"
                )

        return success_count == total_clients

    def test_authentication(self):
        """Test authentication for all configured clients with user feedback"""
        if not self.airwallex_clients:
            frappe.throw("Please configure at least one Airwallex client")

        success_count = 0
        total_clients = len(self.airwallex_clients)
        failed_clients = []

        for client in self.airwallex_clients:
            try:
                api = AirwallexAuthenticator(
                    client_id=client.airwallex_client_id,
                    api_key=client.get_password("airwallex_api_key"),
                    api_url=self.api_url
                )
                response = api.authenticate()

                if response and response.get('token'):
                    success_count += 1
                    frappe.msgprint(
                        f"✅ Authentication successful for client {client.airwallex_client_id}",
                        indicator="green",
                        realtime=True,
                        alert=False
                    )
                else:
                    failed_clients.append(client.airwallex_client_id)
                    frappe.msgprint(
                        f"❌ Authentication failed for client {client.airwallex_client_id}",
                        indicator="red"
                    )
            except Exception as e:
                failed_clients.append(client.airwallex_client_id)
                frappe.msgprint(
                    f"❌ Authentication failed for client {client.airwallex_client_id}: {str(e)}",
                    indicator="red"
                )

        if success_count == total_clients:
            frappe.msgprint(
                f"🎉 All {total_clients} Airwallex clients authenticated successfully!",
                indicator="green",
                realtime=True,
                alert=False
            )
            return True
        else:
            error_message = f"⚠️ {success_count}/{total_clients} clients authenticated successfully"
            if failed_clients:
                error_message += f"\nFailed clients: {', '.join(failed_clients)}"

            frappe.msgprint(
                error_message,
                indicator="orange",
                realtime=True,
                alert=False
            )
            return False

    @frappe.whitelist()
    def start_transaction_sync(self):
        """Start background job for syncing transactions"""
        if not self.from_date or not self.to_date:  # Changed from self.from to self.from_date
            frappe.throw("From and To dates are required for syncing old transactions")

        # Validate date range
        if self.from_date > self.to_date:
            frappe.throw("From date cannot be greater than To date")

        # Update status to indicate sync has started
        # The job takes the feed's lock itself (feed/common.py). It no longer
        # sets last_sync_date: that is the scheduled run's start point, and
        # moving it to now skipped every line since the last scheduled run.
        self.db_set('processed_records', 0)
        self.db_set('total_records', 0)
        self.db_set('sync_progress', 0)
        
        # Enqueue the sync job
        enqueue(
            'bank_integration.airwallex.transaction.sync_transactions',
            queue='long',
            timeout=3600,  # 1 hour timeout
            from_date=str(self.from_date),
            to_date=str(self.to_date),
            setting_name=self.name,
            run="sync old",
        )

        frappe.msgprint(
            "Transaction sync job has been started. You can monitor the progress from this page.",
            indicator="blue", alert=False
        )

    @frappe.whitelist()
    def restart_transaction_sync(self):
        """Restart transaction sync by resetting status and starting new job"""
        if not self.from_date or not self.to_date:  # Changed from self.from to self.from_date
            frappe.throw("From and To dates are required for syncing old transactions")

        # Reset sync status and counters
        self.db_set('sync_status', 'Not Started')
        self.db_set('processed_records', 0)
        self.db_set('total_records', 0)
        self.db_set('sync_progress', 0)

        # Start the sync
        return self.start_transaction_sync()

    @frappe.whitelist()
    def stop_transaction_sync(self):
        """Stop the current transaction sync"""
        try:
            # Update status to stopped
            self.db_set('sync_status', 'Stopped')

            frappe.msgprint(
                "Transaction sync has been marked as stopped. The background job may take a moment to complete.",
                indicator="orange", alert=False
            )

        except Exception as e:
            frappe.log_error(frappe.get_traceback(), "Failed to stop sync job")
            frappe.throw(f"Failed to stop sync job: {str(e)}")

    def update_sync_progress(self, processed, total, status="In Progress"):
        """Update sync progress"""
        progress = (processed / total * 100) if total > 0 else 0

        self.db_set('processed_records', processed)
        self.db_set('total_records', total)
        self.db_set('sync_progress', progress)
        self.db_set('sync_status', status)
        self.db_set('last_sync_date', frappe.utils.now())

        frappe.publish_realtime(
            'transaction_sync_progress',
            {
                'processed': processed,
                'total': total,
                'progress': progress,
                'status': status
            },
            user=frappe.session.user
        )

    def is_skript_enabled(self):
        """Check if Skript integration is enabled"""
        return bool(self.enable_skript)

    @frappe.whitelist()
    def test_skript_authentication(self):
        """Test Skript authentication"""
        if not self.skript_consumer_id:
            frappe.throw("Please configure Skript Consumer ID")
        
        try:
            from bank_integration.skript.api.skript_authenticator import SkriptAuthenticator
            
            auth = SkriptAuthenticator(
                consumer_id=self.skript_consumer_id,
                client_id=self.get_password("skript_client_id"),
                client_secret=self.get_password("skript_client_secret"),
                api_url=self.skript_api_url,
                api_scope=self.skript_api_scope
            )
            
            response = auth.authenticate()
            
            if response and response.get('access_token'):
                frappe.msgprint(
                    "✅ Skript authentication successful! Token cached.",
                    indicator="green",
                    title="Authentication Success"
                )
                return True
            else:
                frappe.msgprint(
                    "❌ Skript authentication failed. Please check your credentials.",
                    indicator="red",
                    title="Authentication Failed"
                )
                return False
        
        except Exception as e:
            frappe.msgprint(
                f"❌ Skript authentication error: {str(e)}",
                indicator="red",
                title="Authentication Error"
            )
            frappe.log_error(frappe.get_traceback(), "Skript Auth Test Error")
            return False

    @frappe.whitelist()
    def fetch_and_create_skript_accounts(self):
        """
        Fetch accounts from Skript API and save directly to database
        This avoids conflicts with background sync jobs
        """
        if not self.enable_skript:
            frappe.throw("Skript integration is not enabled")
        
        from bank_integration.skript.api.skript_accounts import SkriptAccounts
        
        try:
            # Initialize API
            api = SkriptAccounts(
                consumer_id=self.skript_consumer_id,
                client_id=self.get_password("skript_client_id"),
                client_secret=self.get_password("skript_client_secret"),
                api_url=self.skript_api_url,
                api_scope=self.skript_api_scope
            )
            
            # Fetch accounts
            response = api.get_list(size=100)
            
            # Handle response format
            accounts = response if isinstance(response, list) else response.get('items', [])
            
            if not accounts:
                frappe.msgprint("No accounts found in Skript", indicator="blue")
                return {"created": 0, "updated": 0}
            
            created = 0
            updated = 0
            
            # Get existing Skript Account child records from database
            existing_accounts = frappe.get_all(
                "Skript Account",
                filters={"parent": self.name, "parenttype": "Bank Integration Setting"},
                fields=["name", "account_id", "bank_account"]
            )
            existing_map = {acc.account_id: acc for acc in existing_accounts}
            
            for acc in accounts:
                account_id = acc.get('id')
                display_name = acc.get('displayName', 'Unknown Account')
                masked_number = acc.get('maskedNumber', '')
                product_name = acc.get('productName', '')
                data_holder_name = acc.get('dataHolderName', '')
                
                if account_id in existing_map:
                    # Update existing child record directly in database
                    existing = existing_map[account_id]
                    frappe.db.set_value(
                        "Skript Account",
                        existing.name,
                        {
                            "display_name": display_name,
                            "masked_number": masked_number,
                            "product_name": product_name,
                            "data_holder_name": data_holder_name,
                            # Keep existing bank_account mapping
                            "is_mapped": 1 if existing.bank_account else 0
                        },
                        update_modified=False  # Don't update parent's modified timestamp
                    )
                    updated += 1
                else:
                    # Create new child record directly in database
                    child = frappe.get_doc({
                        "doctype": "Skript Account",
                        "parent": self.name,
                        "parenttype": "Bank Integration Setting",
                        "parentfield": "skript_accounts",
                        "account_id": account_id,
                        "display_name": display_name,
                        "masked_number": masked_number,
                        "product_name": product_name,
                        "data_holder_name": data_holder_name,
                        "bank_account": None,
                        "is_mapped": 0
                    })
                    child.insert(ignore_permissions=True)
                    created += 1
            
            # Show summary
            message_parts = []
            if created > 0:
                message_parts.append(f"✅ Added {created} new accounts")
            if updated > 0:
                message_parts.append(f"🔄 Updated {updated} existing accounts")
            
            message_parts.append(f"<br><br>📝 The page will reload. Please map Skript Accounts to ERPNext Bank Accounts in the table.")
            
            frappe.msgprint(
                "<br>".join(message_parts),
                title="Skript Accounts Fetched",
                indicator="green" if created > 0 or updated > 0 else "blue"
            )
            
            return {
                "created": created,
                "updated": updated
            }
        
        except Exception as e:
            frappe.log_error(frappe.get_traceback(), "Skript Accounts Fetch Error")
            frappe.throw(f"Failed to fetch accounts: {str(e)}")

    @frappe.whitelist()
    def validate_skript_account_mapping(self):
        """
        Validate that all Skript accounts are mapped before sync
        Returns list of unmapped accounts or None if all mapped
        """
        if not self.enable_skript:
            return None
        
        if not self.skript_accounts:
            frappe.throw("No Skript accounts found. Please click 'Fetch Accounts' first.")
        
        unmapped = []
        for row in self.skript_accounts:
            if not row.bank_account:
                unmapped.append({
                    'account_id': row.account_id,
                    'display_name': row.display_name,
                    'masked_number': row.masked_number
                })
        
        return unmapped if unmapped else None

    @frappe.whitelist()
    def start_skript_transaction_sync(self):
        """Start background job for syncing Skript transactions"""
        
        # Validate account mapping first
        unmapped = self.validate_skript_account_mapping()
        
        if unmapped:
            # Build error message
            account_list = "<br>".join([
                f"• {acc['display_name']} ({acc['masked_number'] or acc['account_id']})"
                for acc in unmapped
            ])
            
            frappe.throw(
                f"<b>Cannot start sync - Unmapped accounts found:</b><br><br>{account_list}<br><br>"
                f"Please map all Skript accounts to ERPNext Bank Accounts before syncing.",
                title="Unmapped Accounts"
            )
        
        if not self.skript_from_date or not self.skript_to_date:  # Changed from self.from to self.from_date
            frappe.throw("Skript From and To dates are required for syncing old transactions")

        # Validate date range
        if self.skript_from_date > self.skript_to_date:
            frappe.throw("From date cannot be greater than To date")
        
        # The job takes the feed's lock itself (feed/common.py).
        # self.db_set('skript_last_sync_date', frappe.utils.now())
        self.db_set('skript_processed_records', 0)
        self.db_set('skript_total_records', 0)
        self.db_set('skript_sync_progress', 0)
        
        
        # Enqueue the sync job
        from frappe.utils.background_jobs import enqueue
        enqueue(
            'bank_integration.skript.skript_transaction.sync_skript_transactions',
            queue='long',
            timeout=3600,
            setting_name=self.name,
            from_date=str(self.skript_from_date),
            to_date=str(self.skript_to_date),
        )
        
        frappe.msgprint(
            "Skript transaction sync job has been started. You can monitor the progress from this page.",
            indicator="blue",
            alert=False
        )

    def _skript_credentials_changed(self):
        """Check if Skript credentials have changed"""
        if self.is_new():
            return True
        
        # Get the old document
        old_doc = self.get_doc_before_save()
        if not old_doc:
            return True
        
        # Check if any Skript credential fields changed
        if self.skript_api_url != old_doc.skript_api_url:
            return True
        
        if self.skript_access_token_url != old_doc.skript_access_token_url:
            return True
        
        if self.skript_consumer_id != old_doc.skript_consumer_id:
            return True
        
        # Check password fields (they return encrypted values)
        if (self.get_password("skript_client_id") != 
            old_doc.get_password("skript_client_id")):
            return True
        
        if (self.get_password("skript_client_secret") != 
            old_doc.get_password("skript_client_secret")):
            return True
        
        return False

    def test_skript_authentication_silent(self):
        """Test Skript authentication without showing messages - returns True/False"""
        if not self.skript_consumer_id:
            return False
        
        try:
            from bank_integration.skript.api.skript_authenticator import SkriptAuthenticator
            
            auth = SkriptAuthenticator(
                consumer_id=self.skript_consumer_id,
                client_id=self.get_password("skript_client_id"),
                client_secret=self.get_password("skript_client_secret"),
                api_url=self.skript_api_url,
                api_scope=self.skript_api_scope
            )
            
            response = auth.authenticate()
            
            if response and response.get('access_token'):
                return True
            else:
                return False
        
        except Exception as e:
            # Log the error but don't show message
            frappe.log_error(
                f"Skript authentication test failed: {str(e)}",
                "Skript Auth Test"
            )
            return False
    def update_skript_sync_progress(self, processed, total, status="In Progress" , update_last_sync_date=True):
        """Update Skript sync progress without triggering modified timestamp"""
        progress = (processed / total * 100) if total > 0 else 0
        update_data = {
                "skript_processed_records": processed,
                "skript_total_records": total,
                "skript_sync_progress": progress,
                "skript_sync_status": status,
            }
        if update_last_sync_date:
            update_data = {
                **update_data,
                "skript_last_sync_date": frappe.utils.now()
            }
        # Use db_set to avoid document modified conflicts
        frappe.db.set_value(
            "Bank Integration Setting",
            self.name,
            update_data,
            update_modified=False
        )
        
        # Publish realtime updates for UI
        frappe.publish_realtime(
            'skript_sync_progress',
            {
                'processed': processed,
                'total': total,
                'progress': progress,
                'status': status
            },
            user=frappe.session.user
        )
    @frappe.whitelist()
    def restart_skript_transaction_sync(self):
        """Restart Skript transaction sync"""
        if not self.skript_from_date or not self.skript_to_date:
            frappe.throw("Skript From and To dates are required for syncing transactions")
        
        # Reset Skript sync status
        self.db_set('skript_sync_status', 'Not Started')
        self.db_set('skript_processed_records', 0)
        self.db_set('skript_total_records', 0)
        self.db_set('skript_sync_progress', 0)
        
        # Start the sync
        return self.start_skript_transaction_sync()

    @frappe.whitelist()
    def stop_skript_transaction_sync(self):
        """Stop the current Skript transaction sync"""
        try:
            # Update status to stopped
            self.db_set('skript_sync_status', 'Stopped')
            
            frappe.msgprint(
                "Skript transaction sync has been marked as stopped. The background job may take a moment to complete.",
                indicator="orange",
                alert=False
            )
        
        except Exception as e:
            frappe.log_error(frappe.get_traceback(), "Failed to stop Skript sync job")
            frappe.throw(f"Failed to stop sync job: {str(e)}")