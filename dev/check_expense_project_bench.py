"""Expense importer: Project from the job number in the description (E19).

Real Frappe v15 + ERPNext v15 site; the importer's company, supplier, item,
accounts and tax templates are pointed at test ones (names start "FT ").
Run from a bench's sites folder:
  ../env/bin/python /path/to/dev/check_expense_project_bench.py test.local
"""

import sys
import traceback

import frappe

SITE = sys.argv[1] if len(sys.argv) > 1 else "test.local"
OK, BAD = [], []


def check(name, cond, info=""):
    (OK if cond else BAD).append(name)
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {info}"))


def ensure(doctype, name, values):
    if not frappe.db.exists(doctype, name):
        frappe.get_doc(dict(values, doctype=doctype)).insert(ignore_permissions=True)


def fixtures():
    company = "FT Company"  # made by check_feed_bench.py
    abbr = "FTC"
    if not frappe.db.exists("Company", company):
        sys.exit("Run check_feed_bench.py first (it makes FT Company)")
    ensure("UOM", "Unit", {"uom_name": "Unit"})
    ensure("Item Group", "All Item Groups", {"item_group_name": "All Item Groups", "is_group": 1})
    ensure("Supplier Group", "All Supplier Groups", {"supplier_group_name": "All Supplier Groups", "is_group": 1})
    ensure("Supplier Group", "FT Suppliers", {"supplier_group_name": "FT Suppliers", "parent_supplier_group": "All Supplier Groups"})
    ensure("Item Group", "FT Items", {"item_group_name": "FT Items", "parent_item_group": "All Item Groups"})
    ensure("Supplier", "FT Airwallex Expense", {"supplier_name": "FT Airwallex Expense", "supplier_group": "FT Suppliers"})
    ensure("Item", "FT Airwallex generic", {"item_code": "FT Airwallex generic", "item_group": "FT Items",
                                             "stock_uom": "Unit", "is_stock_item": 0})
    expense = frappe.db.get_value("Account", {"company": company, "root_type": "Expense", "is_group": 0}, "name")
    tax_acc = frappe.db.get_value("Account", {"company": company, "account_type": "Tax", "is_group": 0}, "name") \
        or frappe.db.get_value("Account", {"company": company, "root_type": "Liability", "is_group": 0}, "name")
    cc = frappe.db.get_value("Cost Center", {"company": company, "is_group": 0}, "name")
    for title, rate in (("FT GST 10%", 10), ("FT GST Free", 0)):
        if not frappe.db.exists("Purchase Taxes and Charges Template", f"{title} - {abbr}"):
            frappe.get_doc({
                "doctype": "Purchase Taxes and Charges Template", "title": title, "company": company,
                "taxes": [{"charge_type": "On Net Total", "account_head": tax_acc, "rate": rate,
                           "description": title, "category": "Total", "add_deduct_tax": "Add"}],
            }).insert(ignore_permissions=True)
    projects = {}
    for job, title, status in (("0669", "0669 Test vessel", "Open"), ("0622", "0622 Lift frame", "Open"),
                               ("0554", "0554 Old job", "Completed"), ("0000", "0000 Overhead", "Open")):
        name = frappe.db.get_value("Project", {"project_name": title}, "name")
        if not name:
            name = frappe.get_doc({"doctype": "Project", "project_name": title, "company": company,
                                   "status": status}).insert(ignore_permissions=True).name
        frappe.db.set_value("Project", name, "status", status)
        projects[job] = name
    frappe.db.commit()
    return {"company": company, "expense": expense, "cc": cc, "abbr": abbr, "projects": projects}


def run():
    import bank_integration.airwallex.expense_invoice as ei

    fx = fixtures()
    ei.COMPANY = fx["company"]
    ei.SUPPLIER = "FT Airwallex Expense"
    ei.ITEM_CODE = "FT Airwallex generic"
    ei.DEFAULT_EXPENSE_ACCOUNT = fx["expense"]
    ei.COST_CENTER = fx["cc"]
    ei.WAREHOUSE = None
    ei.AUD_TAX_TEMPLATE = f"FT GST 10% - {fx['abbr']}"
    ei.NON_AUD_TAX_TEMPLATE = f"FT GST Free - {fx['abbr']}"
    ei.LETTER_HEAD = None
    frappe.db.sql("delete from `tabPurchase Invoice Item` where parent in (select name from `tabPurchase Invoice` where custom_tm_airwallex_expense_id like 'ft-exp-%%')") \
        if frappe.db.has_column("Purchase Invoice", "custom_tm_airwallex_expense_id") else None
    if not frappe.db.has_column("Purchase Invoice", "custom_tm_airwallex_expense_id"):
        frappe.get_doc({"doctype": "Custom Field", "dt": "Purchase Invoice", "fieldname": "custom_tm_airwallex_expense_id",
                        "label": "Airwallex Expense ID", "fieldtype": "Data", "unique": 1}).insert(ignore_permissions=True)
    frappe.db.sql("delete from `tabPurchase Invoice` where custom_tm_airwallex_expense_id like 'ft-exp-%%'")
    frappe.db.commit()

    projects = ei.open_projects_by_job()
    check("E1 open projects read by job number", projects.get("0669") == [fx["projects"]["0669"]], projects)
    check("E2 a Completed project is not offered", "0554" not in projects, projects.get("0554"))

    def expense(n, desc, amount=110.0):
        return {"id": f"ft-exp-{n}", "description": desc, "billing_amount": amount, "billing_currency": "AUD",
                "created_at": "2026-11-10T01:00:00Z", "status": "APPROVED", "card_id": None}

    pi = ei.create_draft_invoice(expense(1, "0669 lunch with the client"), projects=projects)
    check("E3 invoice gets the project", pi.project == fx["projects"]["0669"], pi.project)
    check("E4 its line gets the project", all(i.project == fx["projects"]["0669"] for i in pi.items))
    check("E5 still a draft with the same amounts as before", pi.docstatus == 0 and abs(pi.grand_total - 110.0) < 0.01, pi.grand_total)
    pi = ei.create_draft_invoice(expense(2, "Tm 622 bolts"), projects=projects)
    check("E6 'Tm 622' finds 0622", pi.project == fx["projects"]["0622"], pi.project)
    pi = ei.create_draft_invoice(expense(3, "Tm 554 fuel"), projects=projects)
    check("E7 job on a Completed project left blank", not pi.project, pi.project)
    pi = ei.create_draft_invoice(expense(4, "0669 and 0622 split"), projects=projects)
    check("E8 two jobs named: left blank for Kim", not pi.project, pi.project)
    pi = ei.create_draft_invoice(expense(5, "Uber to airport"), projects=projects)
    check("E9 no number: left blank", not pi.project and not any(i.project for i in pi.items))
    pi = ei.create_draft_invoice(expense(6, "0000 office milk"), projects=projects)
    check("E10 overhead 0000 set like any open job", pi.project == fx["projects"]["0000"], pi.project)

    # A site rule that refuses a project on a Purchase Invoice must not stop the import.
    orig = ei._insert_with_billno_fallback

    def refuse_project(pi, expense_id):
        if pi.project:
            frappe.throw("Project needs a WBS")
        return orig(pi, expense_id)

    ei._insert_with_billno_fallback = refuse_project
    pi = ei.create_draft_invoice(expense(7, "0669 parts"), projects=projects)
    ei._insert_with_billno_fallback = orig
    check("E11 project refused by a site rule: imported without it, reason in remarks",
          pi.name and not pi.project and "refused" in (pi.remarks or ""), (pi.project, pi.remarks))
    frappe.db.rollback()


def main():
    frappe.init(site=SITE, sites_path=".")
    frappe.connect()
    frappe.set_user("Administrator")
    try:
        run()
    except Exception:
        traceback.print_exc()
        BAD.append("crashed")
    finally:
        frappe.db.rollback()
        frappe.destroy()
    print(f"\n{len(OK)} passed, {len(BAD)} failed")
    sys.exit(1 if BAD else 0)


if __name__ == "__main__":
    main()
