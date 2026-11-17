# Copyright (c) 2026, Thrust Maritime and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class SkriptSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from bank_integration.bank_integration.doctype.skript_account.skript_account import SkriptAccount
		from frappe.types import DF

		accounts: DF.Table[SkriptAccount]
		api_scope: DF.Data | None
		api_url: DF.Data | None
		client_id: DF.Password | None
		client_secret: DF.Password | None
		consumer_id: DF.Data | None
		enable_log: DF.Check
		enabled: DF.Check
		last_sync_date: DF.Datetime | None
		sync_report: DF.LongText | None
		sync_schedule: DF.Literal["Hourly", "Daily", "Weekly", "Monthly"]
		sync_started_on: DF.Datetime | None
		sync_status: DF.Literal["Not Started", "In Progress", "Completed", "Completed with Errors", "Failed"]
		token_url: DF.Data | None
	# end: auto-generated types

	def is_enabled(self):
		return bool(self.enabled)

	def validate(self):
		for row in self.accounts:
			row.is_mapped = 1 if row.bank_account else 0

	@frappe.whitelist()
	def test_connection(self):
		from bank_integration.feed.page import test_connection

		return test_connection("skript")

	@frappe.whitelist()
	def fetch_accounts(self):
		"""Add any Skript account not yet listed. Existing rows are updated in
		place (names and Bank Account kept), never rebuilt."""
		from bank_integration.skript.api.skript_accounts import SkriptAccounts

		api = SkriptAccounts(
			consumer_id=self.consumer_id,
			client_id=self.get_password("client_id"),
			client_secret=self.get_password("client_secret"),
			api_url=self.api_url,
			api_scope=self.api_scope,
		)
		response = api.get_list(size=100)
		found = response if isinstance(response, list) else (response or {}).get("items", [])
		by_id = {row.account_id: row for row in self.accounts}
		added = updated = 0
		for acc in found:
			vals = {
				"display_name": acc.get("displayName") or "Unknown account",
				"masked_number": acc.get("maskedNumber") or "",
				"product_name": acc.get("productName") or "",
				"data_holder_name": acc.get("dataHolderName") or "",
			}
			row = by_id.get(acc.get("id"))
			if row:
				row.update(vals)
				updated += 1
			else:
				self.append("accounts", dict(vals, account_id=acc.get("id")))
				added += 1
		self.save()
		return {"added": added, "updated": updated}
