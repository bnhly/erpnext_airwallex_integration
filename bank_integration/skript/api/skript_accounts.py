import frappe
from .skript_base_api import SkriptBase


class SkriptAccounts(SkriptBase):
    """API wrapper for Skript accounts endpoint"""
    
    def __init__(self, consumer_id, client_id, client_secret, api_url , api_scope="skript/ob-direct-data"):
        super().__init__(consumer_id, client_id, client_secret, api_url, api_scope)
    
    def get_list(self, size=100, ref=None, fields=None, filter=None):
        """
        Get list of accounts for consumer
        GET /consumers/{consumerId}/accounts
        
        Args:
            size: Page size (default 100, max 1000)
            ref: Pagination reference from Link header
            fields: Comma-separated field names for projection
            filter: SQL-like filter expression
        
        Returns:
            list or dict: Account data
        """
        endpoint = f"consumers/{self.consumer_id}/accounts"
        
        params = {"size": size}
        if ref:
            params["ref"] = ref
        if fields:
            params["fields"] = fields
        if filter:
            params["filter"] = filter
        
        return self.get(endpoint=endpoint, params=params)
    
    def get_by_id(self, account_id):
        """
        Get specific account detail
        GET /consumers/{consumerId}/accounts/{accountId}
        
        Args:
            account_id: Skript account ID
        
        Returns:
            dict: Account details
        """
        endpoint = f"consumers/{self.consumer_id}/accounts/{account_id}"
        return self.get(endpoint=endpoint)


def test_get_accounts():
    """
    Test function to fetch accounts
    Usage: bench execute bank_integration.skript.api.accounts.test_get_accounts
    """
    settings = frappe.get_single("Skript Settings")
    
    if not settings.enabled:
        print("Skript is not enabled")
        return
    
    api = SkriptAccounts(
        consumer_id=settings.consumer_id,
        client_id=settings.get_password("client_id"),
        client_secret=settings.get_password("client_secret"),
        api_url=settings.api_url,
        api_scope=settings.api_scope
    )
    
    try:
        accounts = api.get_list(size=10)
        print(f"Fetched accounts: {accounts}")
        return accounts
    except Exception as e:
        print(f"Error: {str(e)}")
        frappe.log_error(frappe.get_traceback(), "Skript Accounts Test")
