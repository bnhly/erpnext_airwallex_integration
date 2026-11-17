# Copyright (c) 2025, Akhilam Inc and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class BankIntegrationLog(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		message: DF.LongText | None
		provider: DF.Literal["", "Airwallex", "Skript"]
		method: DF.SmallText | None
		request_data: DF.Code | None
		request_headers: DF.Code | None
		response_data: DF.LongText | None
		status: DF.Data | None
		status_code: DF.Data | None
		title: DF.Data | None
		traceback: DF.Code | None
		url: DF.SmallText | None
	# end: auto-generated types

	@staticmethod
	def clear_old_logs(days=30):
		"""Called daily by Log Settings. Deletes in batches, so a big backlog
		does not hold one long lock on the table."""
		from bank_integration.feed.maintenance import delete_logs_before

		delete_logs_before(frappe.utils.add_days(frappe.utils.now_datetime(), -int(days or 30)))

	def validate(self):
		if not self.status:
			self.status = "Info"

	# beautify the response_data and request_data fields
	def before_save(self):
		import json

		if self.response_data and isinstance(self.response_data, str):
			try:
				self.response_data = json.dumps(json.loads(self.response_data), sort_keys=True, indent=4)
			except Exception:
				pass

		if self.request_data and isinstance(self.request_data, str):
			try:
				self.request_data = json.dumps(json.loads(self.request_data), sort_keys=True, indent=4)
			except Exception:
				pass


def create_log(message, status="Info", response=None, method=None, payload=None, url=None, status_code=None, provider=None):
	"""A summary or problem row (not an HTTP call)."""
	try:
		log = frappe.get_doc({
			"doctype": "Bank Integration Log",
			"provider": provider,
			"status": status,
			"message": message,
			"response_data": response,
			"request_data": payload,
			"url": url,
			"method": method,
			"status_code": status_code,
		})
		log.insert(ignore_permissions=True)

	except Exception as e:
		frappe.log_error(message=str(e), title="Bank Integration Log Creation Error")
