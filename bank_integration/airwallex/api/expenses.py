import frappe
from bank_integration.airwallex.api.base_api import AirwallexBase


class Expenses(AirwallexBase):
    """API class for the Airwallex Spend Expenses endpoint.

    Read only. This integration never calls the /sync endpoint and never
    reads or writes the Airwallex sync_status field, so it does not conflict
    with any other consumer of the same Airwallex account (for example Xero).
    """

    def __init__(self, client_id=None, api_key=None, api_url=None):
        super().__init__(client_id=client_id, api_key=api_key, api_url=api_url)

    def get_list(self, status=None, sync_status=None, from_created_at=None,
                 to_created_at=None, page=None, legal_entity_id=None):
        """
        List expenses.

        Args:
            status (list[str], optional): e.g. ["APPROVED"]
            sync_status (list[str], optional): not used by this integration
            from_created_at (str, optional): ISO8601, inclusive
            to_created_at (str, optional): ISO8601, exclusive
            page (str, optional): pagination cursor (use page_after from the previous call)
            legal_entity_id (str, optional)

        Returns:
            dict: API response with keys items, page_after, page_before
        """
        params = {}
        if status is not None:
            params["status"] = status
        if sync_status is not None:
            params["sync_status"] = sync_status
        if from_created_at is not None:
            params["from_created_at"] = from_created_at
        if to_created_at is not None:
            params["to_created_at"] = to_created_at
        if page is not None:
            params["page"] = page
        if legal_entity_id is not None:
            params["legal_entity_id"] = legal_entity_id

        return self.get(endpoint="spend/expenses", params=params)

    def get_by_id(self, expense_id):
        """Get a single expense by id."""
        return self.get(endpoint=f"spend/expenses/{expense_id}")

    def iter_all(self, **kwargs):
        """Yield every expense across all pages for the given filters."""
        page = None
        while True:
            response = self.get_list(page=page, **kwargs) or {}
            items = response.get("items") or []
            for item in items:
                yield item
            page = response.get("page_after")
            if not page or not items:
                break


def test_get_approved_expenses():
    # bench execute bank_integration.airwallex.api.expenses.test_get_approved_expenses
    api = Expenses()
    response = api.get_list(status=["APPROVED"])
    print(response)
