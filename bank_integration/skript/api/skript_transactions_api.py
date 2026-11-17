import frappe
from .skript_base_api import SkriptBase


class SkriptTransactions(SkriptBase):
    """API wrapper for Skript transactions endpoint"""
    
    def __init__(self, consumer_id, client_id, client_secret, api_url , api_scope="skript/ob-direct-data"):
        super().__init__(consumer_id, client_id, client_secret, api_url , api_scope)
    
    def get_list_by_account(self, account_id, filter=None, size=100, ref=None, fields=None):
        """
        Get transactions for specific account
        GET /consumers/{consumerId}/accounts/{accountId}/transactions
        
        Args:
            account_id: Skript account ID
            filter: SQL-like filter expression (e.g., "postingDateTime BETWEEN {ts '...'} AND {ts '...'}")
            size: Page size (default 100, max 1000)
            ref: Pagination reference
            fields: Comma-separated field names
        
        Returns:
            list or dict: Transaction data
        """
        endpoint = f"consumers/{self.consumer_id}/accounts/{account_id}/transactions"
        
        params = {"size": size}
        if ref:
            params["ref"] = ref
        if fields:
            params["fields"] = fields
        if filter:
            params["filter"] = filter
        
        return self.get(endpoint=endpoint, params=params)
    
    def get_list_all(self, filter=None, size=100, ref=None, fields=None):
        """
        Get all transactions for consumer (includes accountId in response)
        GET /consumers/{consumerId}/transactions
        
        Args:
            filter: SQL-like filter expression
            size: Page size (default 100, max 1000)
            ref: Pagination reference
            fields: Comma-separated field names
        
        Returns:
            list or dict: Transaction data with accountId
        """
        endpoint = f"consumers/{self.consumer_id}/transactions"
        
        params = {"size": size}
        if ref:
            params["ref"] = ref
        if fields:
            params["fields"] = fields
        if filter:
            params["filter"] = filter
        
        return self.get(endpoint=endpoint, params=params)
    
    def get_by_id(self, account_id, transaction_id):
        """
        Get specific transaction detail
        GET /consumers/{consumerId}/accounts/{accountId}/transactions/{transactionId}
        
        Args:
            account_id: Skript account ID
            transaction_id: Transaction ID
        
        Returns:
            dict: Transaction details with extended data
        """
        endpoint = f"consumers/{self.consumer_id}/accounts/{account_id}/transactions/{transaction_id}"
        return self.get(endpoint=endpoint)
    
    def get_by_id_direct(self, transaction_id):
        """
        Get specific transaction detail without account
        GET /consumers/{consumerId}/transactions/{transactionId}
        
        Args:
            transaction_id: Transaction ID
        
        Returns:
            dict: Transaction details
        """
        endpoint = f"consumers/{self.consumer_id}/transactions/{transaction_id}"
        return self.get(endpoint=endpoint)


def test_get_transactions():
    """
    Test function to fetch transactions
    Usage: bench execute bank_integration.skript.api.transactions.test_get_transactions
    """
    settings = frappe.get_single("Skript Settings")
    
    if not settings.enabled:
        print("Skript is not enabled")
        return
    
    api = SkriptTransactions(
        consumer_id=settings.consumer_id,
        client_id=settings.get_password("client_id"),
        client_secret=settings.get_password("client_secret"),
        api_url=settings.api_url,
        api_scope=settings.api_scope
    )
    
    try:
        # Get recent transactions
        transactions = api.get_list_all(size=5)
        print(f"Fetched transactions: {transactions}")
        return transactions
    except Exception as e:
        print(f"Error: {str(e)}")
        frappe.log_error(frappe.get_traceback(), "Skript Transactions Test")
