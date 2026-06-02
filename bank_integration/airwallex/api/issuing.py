import frappe
from bank_integration.airwallex.api.base_api import AirwallexBase


class Issuing(AirwallexBase):
    """API class for the Airwallex Issuing endpoints. Read-only."""

    def __init__(self, client_id=None, api_key=None, api_url=None):
        super().__init__(client_id=client_id, api_key=api_key, api_url=api_url)

    def get_transaction(self, transaction_id):
        return self.get(endpoint=f"issuing/transactions/{transaction_id}")
