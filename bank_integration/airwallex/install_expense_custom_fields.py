"""
Creates the custom field used for standalone idempotency on Purchase Invoice.

Run once after deploying the app:
    bench --site yoursite execute bank_integration.airwallex.install_expense_custom_fields.create_expense_custom_fields

The field is unique so the same expense can never produce two invoices, and
no_copy so an amended invoice does not carry (and clash on) the value.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def create_expense_custom_fields():
    fields = {
        "Purchase Invoice": [
            {
                "fieldname": "custom_tm_airwallex_expense_id",
                "label": "Airwallex Expense ID",
                "fieldtype": "Data",
                "insert_after": "bill_no",
                "read_only": 1,
                "unique": 1,
                "no_copy": 1,
                "in_standard_filter": 1,
                "translatable": 0,
                "module": "Bank Integration",
            }
        ]
    }
    create_custom_fields(fields, ignore_validate=True)
    frappe.db.commit()
    print("Created custom_tm_airwallex_expense_id on Purchase Invoice")
