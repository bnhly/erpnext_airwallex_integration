import frappe
import frappe.utils
from datetime import datetime, timedelta
from .base_api import AirwallexBase, AirwallexAPIError


class AirwallexAuthenticator(AirwallexBase):
    def __init__(self, client_id=None, api_key=None, api_url=None):
        """Initialize with specific client credentials for authentication"""
        super().__init__(
            client_id=client_id,
            api_key=api_key,
            api_url=api_url,
            use_auth_headers=True
        )

    def authenticate(self):
        """Authenticate with Airwallex API, checking database token first"""
        try:
            # First check if we have a valid token in the database
            cached_token = self._get_cached_token_from_db()
            if cached_token:
                return {"token": cached_token}

            # If no valid token, authenticate and get a new one
            response_data = self.post(
                endpoint="authentication/login",
                json=None
            )

            if response_data and response_data.get('token'):
                self._cache_token_to_db(response_data)
                return response_data
            else:
                # Short error log title
                error_title = f"Auth-{self.client_id[:6]}"
                frappe.log_error(
                    f"Authentication response missing token for client {self.client_id}: {response_data}",
                    error_title
                )
                return None

        except AirwallexAPIError as e:
            # Handle API errors with very short titles
            client_short = self.client_id[:6] if self.client_id else "unknown"
            error_title = f"Auth-{client_short}"

            # Truncate the error message if too long
            error_message = str(e.message)
            if len(error_message) > 300:
                error_message = error_message[:300] + "..."

            frappe.log_error(
                f"Authentication failed for client {self.client_id}: {error_message}",
                error_title
            )
            return None

        except Exception as e:
            # Very short error log title to avoid character limit
            client_short = self.client_id[:6] if self.client_id else "unknown"
            error_title = f"Auth-{client_short}"

            frappe.log_error(
                f"Unexpected authentication error for client {self.client_id}: {str(e)[:300]}",
                error_title
            )
            return None

    # The login token lives in the cache (Redis), keyed by client ID, never
    # in a database field. Before 17 Nov 26 it sat in plain text on the
    # Airwallex Client row (and the save committed mid-run).
    def _cache_key(self):
        return f"bank_integration:airwallex_token:{self.client_id}"

    def _get_cached_token_from_db(self):
        """Cached token, if it has more than 5 minutes left."""
        data = frappe.cache.get_value(self._cache_key(), expires=True) or {}
        if not data.get("token") or not data.get("expiry"):
            return None
        expiry = frappe.utils.get_datetime(data["expiry"])
        if expiry > frappe.utils.now_datetime() + timedelta(minutes=5):
            return data["token"]
        return None

    def _cache_token_to_db(self, token_data):
        expiry = _token_expiry(token_data)
        secs = max(60, int((expiry - frappe.utils.now_datetime()).total_seconds()))
        frappe.cache.set_value(
            self._cache_key(),
            {"token": token_data.get("token"), "expiry": str(expiry)},
            expires_in_sec=secs,
        )

    def _cache_token(self, token_data):
        self._cache_token_to_db(token_data)

    def clear_cached_token(self):
        frappe.cache.delete_value(self._cache_key())

    def get_fresh_token(self):
        """Get a fresh token, bypassing cache"""
        self.clear_cached_token()
        auth_response = self.authenticate()
        if auth_response and auth_response.get('token'):
            return auth_response.get('token')
        return None

    def is_token_valid(self):
        """Check if the current token is still valid without authenticating"""
        cached_token = self._get_cached_token_from_db()
        return cached_token is not None

    def get_valid_token(self):
        """Get a valid token (from cache if available, otherwise authenticate)"""
        auth_response = self.authenticate()
        if auth_response and auth_response.get('token'):
            return auth_response.get('token')
        return None

    def handle_token_invalidation(self):
        """Handle when a token is found to be invalid - clear cache and get fresh token"""
        # A token running out is normal; it was an Error Log row every time.
        frappe.logger("bank_integration").info(
            f"Airwallex token refused for client {self.client_id[:6]}, getting a fresh one"
        )
        self.clear_cached_token()
        return self.get_valid_token()


def _token_expiry(token_data):
    """When the login token stops working, in site time.

    Airwallex answers {token, expires_at} (UTC), not expires_in. The old code
    read expires_in, found none and assumed an hour, but the token lasts 30
    minutes, so every run after the first half hour hit a 401 first. Takes
    expires_at when present, else expires_in, else 25 minutes.
    """
    now = frappe.utils.now_datetime()
    raw = token_data.get("expires_at")
    if raw:
        try:
            import pytz

            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = pytz.UTC.localize(dt)
            tz = pytz.timezone(frappe.utils.get_system_timezone())
            return dt.astimezone(tz).replace(tzinfo=None)
        except Exception:
            pass
    try:
        secs = int(token_data.get("expires_in") or 0)
    except (TypeError, ValueError):
        secs = 0
    return now + timedelta(seconds=secs or 25 * 60)
