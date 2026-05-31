import frappe
from bank_integration.airwallex.api.base_api import AirwallexBase


class Deposits(AirwallexBase):
    """API class for the Airwallex Deposits endpoint. Read-only."""

    def __init__(self, client_id=None, api_key=None, api_url=None):
        super().__init__(client_id=client_id, api_key=api_key, api_url=api_url)

    def get_by_id(self, deposit_id):
        return self.get(endpoint=f"deposits/{deposit_id}")
