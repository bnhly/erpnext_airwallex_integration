import frappe
from bank_integration.airwallex.api.base_api import AirwallexBase, AirwallexAPIError


class FinancialTransactions(AirwallexBase):
    """API class for Airwallex Financial Transactions endpoint"""

    def __init__(self, client_id=None, api_key=None, api_url=None):
        super().__init__(client_id=client_id, api_key=api_key, api_url=api_url)

    def get_list(self, batch_id=None, currency=None, from_created_at=None,
                 page_num=None, page_size=None, source_id=None, status=None,
                 to_created_at=None):
        """
        Get list of financial transactions

        Args:
            batch_id (str, optional): Batch ID of the financial transaction
            currency (str, optional): The currency (3-letter ISO-4217 code) of the financial transaction
            from_created_at (str, optional): The start time of created_at in ISO8601 format (e.g., '2023-10-14T10:30:00Z')
            page_num (int, optional): Page number, starts from 0
            page_size (int, optional): Number of results per page, default is 100, max is 1000
            source_id (str, optional): The source ID of the transaction
            status (str, optional): Status of the financial transaction, one of: PENDING, SETTLED
            to_created_at (str, optional): The end time of created_at in ISO8601 format (e.g., '2023-10-14T15:30:00Z')

        Returns:
            dict: API response containing list of financial transactions
        """
        params = {}

        # Add parameters only if they are provided
        if batch_id is not None:
            params['batch_id'] = batch_id
        if currency is not None:
            params['currency'] = currency
        if from_created_at is not None:
            params['from_created_at'] = from_created_at
        if page_num is not None:
            params['page_num'] = page_num
        if page_size is not None:
            params['page_size'] = page_size
        if source_id is not None:
            params['source_id'] = source_id
        if status is not None:
            params['status'] = status
        if to_created_at is not None:
            params['to_created_at'] = to_created_at

        return self.get(endpoint="financial_transactions", params=params)

    PAGE_SIZE = 1000
    MAX_PAGES = 200

    def iter_all(self, from_created_at=None, to_created_at=None, page_size=None):
        """Yield every financial transaction in the window, across all pages.

        The list call returns one page only (100 lines unless asked). Before
        17 Nov 26 the sync read that first page and nothing more, so a window
        with more lines than that lost the rest. Handles both of Airwallex's
        paging styles: page_num + has_more, and the newer page cursor
        (page_after).
        """
        size = page_size or self.PAGE_SIZE
        page_num = 0
        cursor = None
        seen = set()
        for _ in range(self.MAX_PAGES):
            params = {"page_size": size}
            if from_created_at:
                params["from_created_at"] = from_created_at
            if to_created_at:
                params["to_created_at"] = to_created_at
            if cursor:
                params["page"] = cursor
            else:
                params["page_num"] = page_num
            data = self.get(endpoint="financial_transactions", params=params)
            if not isinstance(data, dict):
                data = {}
            items = data.get("items") or data.get("data") or []
            fresh = 0
            for item in items:
                key = item.get("id")
                if key in seen:
                    continue
                seen.add(key)
                fresh += 1
                yield item
            if not items or not fresh:
                return
            if data.get("page_after"):
                cursor = data["page_after"]
                continue
            # has_more False is the end. When the reply does not say, ask for
            # the next page anyway: a short page is not proof of the end if
            # Airwallex caps page_size below what was asked. A page with
            # nothing new stops it (above).
            if data.get("has_more") is False:
                return
            page_num += 1
        raise AirwallexAPIError(
            f"Stopped after {self.MAX_PAGES} pages; narrow the date range", 500
        )

    def get_by_id(self, transaction_id):
        """
        Get a specific financial transaction by ID

        Args:
            transaction_id (str): The ID of the financial transaction

        Returns:
            dict: API response containing the financial transaction details
        """
        return self.get(endpoint=f"financial_transactions/{transaction_id}")

def test_get_transactions():
    # bench execute bank_integration.airwallex.api.financial_transactions.test_get_transactions
    ft_api = FinancialTransactions()
    response = ft_api.get_list(page_num=0, page_size=10)
    print(response)

def test_get_transactions_with_dates():
    # bench execute bank_integration.airwallex.api.financial_transactions.test_get_transactions_with_dates
    import frappe
    from datetime import datetime, timedelta
    import pytz

    # Get settings to use the helper method
    settings = frappe.get_single("Airwallex Settings")

    # Test with recent dates in local timezone
    local_tz = pytz.timezone(frappe.utils.get_system_timezone())
    end_date = datetime.now(local_tz)
    start_date = end_date - timedelta(days=7)

    print(f"Local timezone: {frappe.utils.get_system_timezone()}")
    print(f"Start date (local): {start_date}")
    print(f"End date (local): {end_date}")

    # Convert to ISO8601 format (will be converted to UTC)
    from_date_iso = settings._to_iso8601(start_date)
    to_date_iso = settings._to_iso8601(end_date)

    print(f"From date ISO8601 (UTC): {from_date_iso}")
    print(f"To date ISO8601 (UTC): {to_date_iso}")

    # Also test with naive datetime (no timezone info)
    naive_end = datetime.now()
    naive_start = naive_end - timedelta(days=7)

    print(f"\nTesting with naive datetime:")
    print(f"Naive start date: {naive_start}")
    print(f"Naive end date: {naive_end}")

    naive_from_iso = settings._to_iso8601(naive_start)
    naive_to_iso = settings._to_iso8601(naive_end)

    print(f"Naive from date ISO8601 (UTC): {naive_from_iso}")
    print(f"Naive to date ISO8601 (UTC): {naive_to_iso}")

    ft_api = FinancialTransactions()
    response = ft_api.get_list(
        from_created_at=from_date_iso,
        to_created_at=to_date_iso,
        page_size=5
    )
    print(f"\nResponse: {response}")

def test_token_refresh():
    # bench execute bank_integration.airwallex.api.financial_transactions.test_token_refresh
    import frappe
    from bank_integration.airwallex.api.airwallex_authenticator import AirwallexAuthenticator

    # Get first client settings
    settings = frappe.get_single("Airwallex Settings")
    if not settings.clients:
        print("No clients configured")
        return

    client = settings.clients[0]

    # Test the token refresh mechanism
    auth = AirwallexAuthenticator(
        client_id=client.airwallex_client_id,
        api_key=client.get_password("airwallex_api_key"),
        api_url=settings.api_url
    )

    # Force clear any existing token
    auth.clear_cached_token()
    print("Cleared existing token")

    # Get a fresh token
    token = auth.get_valid_token()
    print(f"Got fresh token: {token[:20]}..." if token else "Failed to get token")

    # Test with FinancialTransactions API
    ft_api = FinancialTransactions(
        client_id=client.airwallex_client_id,
        api_key=client.get_password("airwallex_api_key"),
        api_url=settings.api_url
    )

    try:
        response = ft_api.get_list(page_size=1)
        print("API call successful - token refresh mechanism working")
        print(f"Response: {response}")
    except Exception as e:
        print(f"API call failed: {str(e)}")
        print("This might indicate an authentication issue")
