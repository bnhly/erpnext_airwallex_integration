"""Server side of the Bank Feeds page (/app/bank-feeds).

overview        per provider: on / off, schedule, status, last good run, the
                last run's failures; per Bank Account: last line, lines in
                the last 30 days, weekdays with no line, drafts, failures.
test_connection log in to the provider and read one page. Writes nothing.
sync_now        run the provider now, whatever its schedule (background).
Catch-up uses feed.tools.catch_up.

System Manager or Accounts Manager.
"""

from datetime import timedelta

import frappe
from frappe.utils import add_days, getdate, nowdate

from bank_integration.feed import common

ROLES = ("System Manager", "Accounts Manager")
DAYS = 30


def _allowed():
    if frappe.session.user == "Administrator":
        return
    if not set(ROLES) & set(frappe.get_roles()):
        frappe.throw("Only a System Manager or Accounts Manager can use this.", frappe.PermissionError)


def _provider(p):
    p = (p or "").lower()
    if p not in common.DOCTYPE:
        frappe.throw("provider is airwallex or skript")
    return p


def _accounts():
    """[(provider, bank_account, label)] fed by each provider."""
    out = []
    aw = frappe.get_single(common.AIRWALLEX)
    for row in aw.clients or []:
        if row.bank_account:
            out.append(("airwallex", row.bank_account, (row.airwallex_client_id or "")[:8]))
    sk = frappe.get_single(common.SKRIPT)
    for row in sk.accounts or []:
        if row.bank_account:
            out.append(("skript", row.bank_account, row.display_name or row.masked_number or row.account_id))
    return out


@frappe.whitelist()
def overview():
    _allowed()
    today = getdate(nowdate())
    since = add_days(today, -DAYS)
    providers = []
    fails_by_account = {}
    for key, dt in common.DOCTYPE.items():
        doc = frappe.get_single(dt)
        rep = common.report_of(dt) or {}
        for f in rep.get("failed") or []:
            fails_by_account.setdefault(f.get("bank_account") or "", []).append(f)
        providers.append({
            "key": key,
            "doctype": dt,
            "label": "Airwallex" if key == "airwallex" else "Skript",
            "enabled": doc.enabled,
            "schedule": doc.sync_schedule,
            "status": doc.sync_status,
            "started": doc.sync_started_on,
            "last_good": doc.last_sync_date,
            "floor": doc.get("reconciled_to"),
            "report": {
                "run": rep.get("run"),
                "ended": rep.get("ended"),
                "created": rep.get("created"),
                "failed": len(rep.get("failed") or []) + int(rep.get("failed_more") or 0),
                "problems": rep.get("problems") or [],
                "window": rep.get("window"),
            },
        })

    accounts = []
    for key, ba, label in _accounts():
        stats = frappe.db.sql(
            """select max(date) last_date, max(creation) last_made,
                      sum(case when date >= %(since)s and docstatus=1 then 1 else 0 end) recent,
                      sum(case when docstatus=0 then 1 else 0 end) drafts
               from `tabBank Transaction` where bank_account=%(ba)s""",
            {"ba": ba, "since": since},
            as_dict=True,
        )[0]
        days = {
            getdate(d)
            for (d,) in frappe.db.sql(
                """select distinct date from `tabBank Transaction`
                   where bank_account=%s and docstatus=1 and date >= %s""",
                (ba, since),
            )
        }
        gaps = []
        d = since
        while d < today:
            if d.weekday() < 5 and d not in days:
                gaps.append(str(d))
            d = d + timedelta(days=1)
        currency = frappe.db.get_value(
            "Account", frappe.db.get_value("Bank Account", ba, "account"), "account_currency"
        )
        accounts.append({
            "provider": key,
            "bank_account": ba,
            "source": label,
            "currency": currency,
            "last_date": stats.last_date,
            "last_made": stats.last_made,
            "recent": int(stats.recent or 0),
            "drafts": int(stats.drafts or 0),
            "gaps": gaps,
            "failed": fails_by_account.get(ba, [])[:50],
        })
    no_account = frappe.db.sql(
        """select currency, count(*) n from `tabBank Transaction`
           where ifnull(bank_account,'')='' and ifnull(transaction_id,'')!='' and docstatus<2
           group by currency""",
        as_dict=True,
    )
    return {"providers": providers, "accounts": accounts, "no_account": no_account, "days": DAYS,
            "logs": frappe.db.count("Bank Integration Log")}


@frappe.whitelist()
def test_connection(provider):
    _allowed()
    provider = _provider(provider)
    if provider == "airwallex":
        from bank_integration.airwallex.api.base_api import AirwallexAPIError
        from bank_integration.airwallex.api.financial_transactions import FinancialTransactions
        from bank_integration.airwallex.transaction import client_routes

        settings = frappe.get_single(common.AIRWALLEX)
        logins, problems = client_routes(settings)
        out = []
        for lg in logins:
            item = {"login": lg["client_id"][:8], "routes": {c: r.bank_account for c, r in lg["routes"].items()}}
            try:
                api = FinancialTransactions(
                    client_id=lg["client_id"],
                    api_key=lg["key_row"].get_password("airwallex_api_key"),
                    api_url=settings.api_url,
                )
                api.get_list(page_num=0, page_size=1)
                item["ok"] = True
            except AirwallexAPIError as e:
                item["ok"] = False
                item["message"] = str(e.message)[:300]
            except Exception as e:  # noqa: BLE001
                item["ok"] = False
                item["message"] = common.clean_reason(e)
            out.append(item)
        return {"logins": out, "problems": problems}

    from bank_integration.skript.api.skript_accounts import SkriptAccounts

    s = frappe.get_single(common.SKRIPT)
    try:
        api = SkriptAccounts(
            consumer_id=s.consumer_id,
            client_id=s.get_password("client_id"),
            client_secret=s.get_password("client_secret"),
            api_url=s.api_url,
            api_scope=s.api_scope,
        )
        res = api.get_list(size=100)
        found = res if isinstance(res, list) else (res or {}).get("items", [])
        listed = {r.account_id for r in s.accounts}
        return {"logins": [{"login": "Skript", "ok": True,
                            "accounts": len(found),
                            "not_listed": [a.get("displayName") for a in found if a.get("id") not in listed]}]}
    except Exception as e:  # noqa: BLE001
        return {"logins": [{"login": "Skript", "ok": False, "message": common.clean_reason(e)}]}


@frappe.whitelist()
def sync_now(provider):
    _allowed()
    provider = _provider(provider)
    path = {
        "airwallex": "bank_integration.airwallex.transaction.sync_scheduled_transactions",
        "skript": "bank_integration.skript.skript_transaction.sync_scheduled_transactions_skript",
    }[provider]
    if not frappe.db.get_single_value(common.DOCTYPE[provider], "enabled"):
        frappe.throw(f"{provider.title()} is off. Turn it on in its settings first.")
    frappe.enqueue(
        path,
        queue="long",
        timeout=3600,
        job_id=f"bank_feed_sync_now_{provider}",
        deduplicate=True,
        force=True,
    )
    return {"started": True}
