# Copyright (c) 2026, Thrust Maritime and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class AirwallexSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from bank_integration.bank_integration.doctype.airwallex_client.airwallex_client import AirwallexClient
		from bank_integration.bank_integration.doctype.transaction_type_filter.transaction_type_filter import TransactionTypeFilter
		from frappe.types import DF

		api_url: DF.Data | None
		clients: DF.Table[AirwallexClient]
		enable_log: DF.Check
		enabled: DF.Check
		last_sync_date: DF.Datetime | None
		sync_report: DF.LongText | None
		sync_schedule: DF.Literal["Hourly", "Daily", "Weekly", "Monthly"]
		sync_started_on: DF.Datetime | None
		sync_status: DF.Literal["Not Started", "In Progress", "Completed", "Completed with Errors", "Failed"]
		transaction_type_filters: DF.Table[TransactionTypeFilter]
	# end: auto-generated types

	def is_enabled(self):
		return bool(self.enabled)

	def validate(self):
		seen = {}
		for row in self.clients:
			row.airwallex_client_id = (row.airwallex_client_id or "").strip()
			if row.bank_account in seen:
				frappe.throw(f"Row {row.idx}: {row.bank_account} is already on row {seen[row.bank_account]}")
			seen[row.bank_account] = row.idx

	def should_sync_transaction(self, transaction_type):
		"""Include / Exclude rules. No rules = everything. Any Include rule =
		only the included types."""
		rules = self.transaction_type_filters or []
		if not rules:
			return True
		for rule in rules:
			if rule.transaction_type == transaction_type:
				return rule.filter_action == "Include"
		return not any(r.filter_action == "Include" for r in rules)

	def _to_iso8601(self, dt):
		"""A datetime (site time when naive) as UTC ISO 8601 with Z."""
		import pytz
		from frappe.utils import get_datetime

		if not dt:
			return None
		dt = get_datetime(dt) if isinstance(dt, str) else dt
		if dt.tzinfo is None:
			dt = pytz.timezone(frappe.utils.get_system_timezone()).localize(dt)
		return dt.astimezone(pytz.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

	@frappe.whitelist()
	def test_connection(self):
		from bank_integration.feed.page import test_connection

		return test_connection("airwallex")
