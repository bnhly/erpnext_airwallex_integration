"""Move Bank Integration Setting into Airwallex Settings and Skript Settings.

17 Nov 26. One settings record held both providers. This copies each value
across, RE-PARENTS the existing child rows in place (same row names, same
encrypted API keys: never rebuilt from dicts), moves the Skript client ID and
secret in __Auth, drops the cached tokens that sat in plain fields, then
removes the old doctype once nothing is left on it.

Safe to run twice: a field already set on the new record is not overwritten,
and rows already moved are not found again.
"""

import frappe

OLD = "Bank Integration Setting"
AW = "Airwallex Settings"
SK = "Skript Settings"

AW_MAP = {
    "enable_airwallex": "enabled",
    "api_url": "api_url",
    "sync_schedule": "sync_schedule",
    "enable_log": "enable_log",
    "sync_status": "sync_status",
    "sync_started_on": "sync_started_on",
    "last_sync_date": "last_sync_date",
    "sync_report": "sync_report",
}
SK_MAP = {
    "enable_skript": "enabled",
    "skript_api_url": "api_url",
    "skript_access_token_url": "token_url",
    "skript_api_scope": "api_scope",
    "skript_consumer_id": "consumer_id",
    "skript_sync_schedule": "sync_schedule",
    "enable_log": "enable_log",
    "skript_sync_status": "sync_status",
    "skript_sync_started_on": "sync_started_on",
    "skript_last_sync_date": "last_sync_date",
    "skript_sync_report": "sync_report",
}
CHILDREN = (
    ("Airwallex Client", "airwallex_clients", AW, "clients"),
    ("Transaction Type Filter", "transaction_type_filters", AW, "transaction_type_filters"),
    ("Skript Account", "skript_accounts", SK, "accounts"),
)
SECRETS = (("skript_client_id", "client_id"), ("skript_client_secret", "client_secret"))


def execute():
    old = dict(frappe.db.sql("select field, value from `tabSingles` where doctype=%s", OLD))
    moved = {"values": [], "rows": {}, "secrets": []}

    for src, (new_dt, mapping) in {"aw": (AW, AW_MAP), "sk": (SK, SK_MAP)}.items():
        have = dict(frappe.db.sql("select field, value from `tabSingles` where doctype=%s", new_dt))
        for old_f, new_f in mapping.items():
            val = old.get(old_f)
            if val in (None, "") or have.get(new_f) not in (None, ""):
                continue
            frappe.db.set_single_value(new_dt, new_f, val, update_modified=False)
            moved["values"].append(f"{new_dt}.{new_f}")

    for child, old_field, new_dt, new_field in CHILDREN:
        if not frappe.db.table_exists(child):
            continue
        n = frappe.db.sql(
            f"select count(*) from `tab{child}` where parenttype=%s and parentfield=%s",
            (OLD, old_field),
        )[0][0]
        if n:
            frappe.db.sql(
                f"""update `tab{child}` set parent=%s, parenttype=%s, parentfield=%s
                    where parenttype=%s and parentfield=%s""",
                (new_dt, new_dt, new_field, OLD, old_field),
            )
        moved["rows"][child] = n

    for old_f, new_f in SECRETS:
        exists_new = frappe.db.sql(
            "select count(*) from `__Auth` where doctype=%s and name=%s and fieldname=%s", (SK, SK, new_f)
        )[0][0]
        if exists_new:
            continue
        frappe.db.sql(
            """update `__Auth` set doctype=%s, name=%s, fieldname=%s
               where doctype=%s and name=%s and fieldname=%s""",
            (SK, SK, new_f, OLD, OLD, old_f),
        )
        moved["secrets"].append(new_f)

    # Tokens no longer live in fields (they are in the cache now).
    cols = frappe.db.get_table_columns("Airwallex Client")
    if "token" in cols:
        frappe.db.sql("update `tabAirwallex Client` set token=NULL")
    if "token_expiry" in cols:
        frappe.db.sql("update `tabAirwallex Client` set token_expiry=NULL")

    left = 0
    for child, old_field, _dt, _f in CHILDREN:
        if frappe.db.table_exists(child):
            left += frappe.db.sql(f"select count(*) from `tab{child}` where parenttype=%s", (OLD,))[0][0]
    secrets_left = frappe.db.sql(
        "select count(*) from `__Auth` where doctype=%s and fieldname in ('skript_client_id','skript_client_secret')",
        (OLD,),
    )[0][0]

    if not left and not secrets_left:
        if frappe.db.exists("DocType", OLD):
            frappe.delete_doc("DocType", OLD, force=True, ignore_permissions=True)
        # The old values (the plain Skript token among them) go with it.
        frappe.db.delete("Singles", {"doctype": OLD})
        frappe.db.delete("__Auth", {"doctype": OLD})
        moved["removed_old"] = True
    frappe.db.commit()
    print("bank_integration split_provider_settings:", moved, "left on old:", left, secrets_left)
