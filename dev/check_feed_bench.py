"""Bank feed checks on a REAL Frappe v15 + ERPNext v15 site.

Airwallex and Skript are replaced by recorded JSON (requests.request is
stubbed); everything else is the real app on the real site: Bank Account,
Bank Transaction insert and submit, savepoints, Property Setters, the lock
rows in tabSingles, Log Settings.

Run from a bench's sites folder:
  ../env/bin/python /path/to/dev/check_feed_bench.py test.local

It makes its own company, accounts and bank accounts (names start "FT ")
and deletes every Bank Transaction it made at the start of each run.
"""

import json
import sys
import traceback
from datetime import timedelta
from types import SimpleNamespace

import frappe

SITE = sys.argv[1] if len(sys.argv) > 1 else "test.local"
OK = []
BAD = []


def check(name, cond, info=""):
    (OK if cond else BAD).append(name)
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {info}"))


# ---------------------------------------------------------------- fake HTTP

class FakeResp:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data
        self.text = json.dumps(data)

    def json(self):
        return self._data


class FakeAirwallex:
    """Answers the endpoints the feed calls. lines = list of txn dicts."""

    def __init__(self):
        self.lines = []
        self.calls = []
        self.page_size_cap = None
        self.style = "has_more"   # or "cursor"

    def __call__(self, method, url, params=None, json=None, headers=None, **kw):
        self.calls.append((method, url, dict(params or {}), dict(headers or {})))
        if url.endswith("authentication/login"):
            return FakeResp(201, {"token": "TOKEN-SECRET-123", "expires_at": "2099-01-01T00:00:00+0000"})
        if "Authorization" not in (headers or {}):
            return FakeResp(401, {"code": "unauthorized"})
        if url.endswith("/financial_transactions"):
            p = params or {}
            frm, to = p.get("from_created_at"), p.get("to_created_at")
            rows = [t for t in self.lines if (not frm or t["created_at"] >= frm) and (not to or t["created_at"] <= to)]
            rows.sort(key=lambda t: t["created_at"], reverse=True)
            size = int(p.get("page_size") or 100)
            if self.page_size_cap:
                size = min(size, self.page_size_cap)
            if self.style == "cursor":
                start = int(p.get("page") or 0)
                chunk = rows[start:start + size]
                nxt = str(start + size) if start + size < len(rows) else None
                return FakeResp(200, {"items": chunk, "page_after": nxt})
            num = int(p.get("page_num") or 0)
            chunk = rows[num * size:(num + 1) * size]
            if getattr(self, "no_has_more", False):
                return FakeResp(200, {"items": chunk})
            return FakeResp(200, {"items": chunk, "has_more": (num + 1) * size < len(rows)})
        if "spend/expenses" in url:
            return FakeResp(200, {"items": [], "page_after": None})
        if "/issuing/transactions/" in url or "/deposits/" in url or "/transfers/" in url:
            return FakeResp(404, {"code": "not_found"})
        if "/financial_transactions/" in url:
            tid = url.rsplit("/", 1)[1]
            for t in self.lines:
                if t["id"] == tid:
                    return FakeResp(200, t)
            return FakeResp(404, {"code": "not_found"})
        return FakeResp(404, {"code": "no_route", "url": url})


class FakeSkript:
    def __init__(self):
        self.lines = []
        self.calls = []
        self.size_cap = None

    def request(self, method, url, params=None, json=None, headers=None, **kw):
        self.calls.append((method, url, dict(params or {})))
        if url.endswith("/transactions"):
            import re

            f = (params or {}).get("filter") or ""
            m = re.findall(r"\{ts '([^']+)'\}", f)
            lo, hi = (m + [None, None])[:2]
            rows = []
            from bank_integration.skript.skript_utils import format_datetime_for_skript_filter, parse_skript_to_system_timezone

            for t in self.lines:
                ts = format_datetime_for_skript_filter(parse_skript_to_system_timezone(t["postingDateTime"]))
                if (not lo or ts >= lo) and (not hi or ts <= hi):
                    rows.append(t)
            rows.sort(key=lambda t: t["postingDateTime"])
            size = int((params or {}).get("size") or 100)
            if self.size_cap:
                size = min(size, self.size_cap)
            return FakeResp(200, {"items": rows[:size]})
        return FakeResp(404, {})

    def post(self, url, data=None, headers=None, **kw):
        return FakeResp(200, {"access_token": "SKRIPT-TOKEN-XYZ", "expires_in": 3600})


# ---------------------------------------------------------------- fixtures

def ensure_setup():
    if frappe.db.get_single_value("System Settings", "time_zone") != "Australia/Melbourne":
        frappe.db.set_single_value("System Settings", "time_zone", "Australia/Melbourne")
        frappe.db.commit()
        frappe.clear_cache()
    company = "FT Company"
    if not frappe.db.exists("Warehouse Type", "Transit"):
        frappe.get_doc({"doctype": "Warehouse Type", "name": "Transit"}).insert(ignore_permissions=True)
    if not frappe.db.exists("Company", company):
        frappe.get_doc({
            "doctype": "Company", "company_name": company, "abbr": "FTC",
            "default_currency": "AUD", "country": "Australia",
            "chart_of_accounts": "Standard",
        }).insert(ignore_permissions=True)
    parent = frappe.db.get_value("Account", {"company": company, "account_type": "Bank", "is_group": 1}, "name") \
        or frappe.db.get_value("Account", {"company": company, "account_name": "Bank Accounts"}, "name")
    if not frappe.db.exists("Bank", "FT Airwallex"):
        frappe.get_doc({"doctype": "Bank", "bank_name": "FT Airwallex"}).insert(ignore_permissions=True)
    if not frappe.db.exists("Bank", "FT CBA"):
        frappe.get_doc({"doctype": "Bank", "bank_name": "FT CBA"}).insert(ignore_permissions=True)
    for cur in ("USD",):
        if not frappe.db.exists("Currency", cur):
            frappe.get_doc({"doctype": "Currency", "currency_name": cur, "enabled": 1}).insert()
        frappe.db.set_value("Currency", cur, "enabled", 1)
    accts = {}
    for label, cur, bank in (("FT AWX AUD", "AUD", "FT Airwallex"), ("FT AWX USD", "USD", "FT Airwallex"),
                             ("FT CBA", "AUD", "FT CBA"), ("FT Judo", "AUD", "FT CBA")):
        acc = frappe.db.get_value("Account", {"account_name": label, "company": company}, "name")
        if not acc:
            acc = frappe.get_doc({
                "doctype": "Account", "account_name": label, "parent_account": parent, "company": company,
                "account_type": "Bank", "account_currency": cur, "is_group": 0,
            }).insert(ignore_permissions=True).name
        ba = frappe.db.get_value("Bank Account", {"account_name": label}, "name")
        if not ba:
            ba = frappe.get_doc({
                "doctype": "Bank Account", "account_name": label, "bank": bank, "account": acc,
                "is_company_account": 1, "company": company,
            }).insert(ignore_permissions=True).name
        accts[label] = ba
    frappe.db.commit()
    return accts


def reset_state(accts, with_skript=False):
    frappe.db.sql("delete from `tabBank Transaction` where transaction_id like 'ft-%%'")
    frappe.db.sql("delete from `tabProperty Setter` where doc_type='Bank Transaction' and field_name='reference_number'")
    frappe.clear_cache(doctype="Bank Transaction")
    s = frappe.get_single("Airwallex Settings")
    s.enabled = 1
    s.api_url = "https://api.airwallex.test/api/v1"
    s.sync_schedule = "Hourly"
    s.enable_log = 0
    s.set("clients", [])
    # USD row first on purpose: the order must not matter any more.
    s.append("clients", {"bank_account": accts["FT AWX USD"], "airwallex_client_id": "CLIENT-ONE-LOGIN",
                                   "airwallex_api_key": "LONGLIFE-API-KEY-XYZ"})
    s.append("clients", {"bank_account": accts["FT AWX AUD"], "airwallex_client_id": "CLIENT-ONE-LOGIN",
                                   "airwallex_api_key": "LONGLIFE-API-KEY-XYZ"})
    s.set("transaction_type_filters", [])
    s.save(ignore_permissions=True)
    k = frappe.get_single("Skript Settings")
    k.enabled = 1 if with_skript else 0
    k.api_url = "https://skript.test/v1"
    k.token_url = "https://skript.test/token"
    k.consumer_id = "CONSUMER"
    k.client_id = "SK-ID"
    k.client_secret = "SK-SECRET"
    k.sync_schedule = "Hourly"
    k.set("accounts", [])
    k.append("accounts", {"account_id": "acc-cba", "display_name": "CBA Transaction", "bank_account": accts["FT CBA"]})
    k.append("accounts", {"account_id": "acc-judo", "display_name": "Judo LOC 5803", "bank_account": accts["FT Judo"]})
    k.append("accounts", {"account_id": "acc-new", "display_name": "New account", "bank_account": None})
    k.save(ignore_permissions=True)
    for dt in ("Airwallex Settings", "Skript Settings"):
        for f, v in (("sync_status", "Completed"), ("sync_started_on", None), ("last_sync_date", None),
                     ("sync_report", None)):
            frappe.db.set_single_value(dt, f, v, update_modified=False)
    frappe.cache.delete_value("bank_integration:airwallex_token:CLIENT-ONE-LOGIN")
    frappe.cache.delete_value("bank_integration:skript_token")
    frappe.db.commit()


def iso(dt):
    import pytz

    tz = pytz.timezone(frappe.utils.get_system_timezone())
    return tz.localize(dt).astimezone(pytz.UTC).strftime("%Y-%m-%dT%H:%M:%S+0000")


def line(n, when, cur="AUD", net=-10.0, ttype="CARD_PURCHASE", batch="b"):
    return {"id": f"ft-{n}", "created_at": iso(when), "currency": cur, "net": net, "amount": net,
            "transaction_type": ttype, "source_type": "CARD_PURCHASE", "source_id": f"src-{n}",
            "status": "SETTLED", "description": f"line {n}", "batch_id": batch}


def bt(tid):
    return frappe.db.get_value("Bank Transaction", {"transaction_id": tid},
                               ["name", "docstatus", "bank_account", "date"], as_dict=True)


# ---------------------------------------------------------------- checks

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
    if BAD:
        print("FAILED:", ", ".join(BAD))
        sys.exit(1)


def run():
    import bank_integration.airwallex.api.base_api as base_api
    import bank_integration.skript.api.skript_authenticator as sk_auth
    import bank_integration.skript.api.skript_base_api as sk_base
    from bank_integration.airwallex.transaction import sync_scheduled_transactions, sync_transactions

    accts = ensure_setup()
    now = frappe.utils.now_datetime()
    awx = FakeAirwallex()
    base_api.requests = SimpleNamespace(request=awx)

    # ---- A1 one login, two rows, three currencies, many pages
    reset_state(accts)
    awx.page_size_cap = 7
    awx.lines = [line(i, now - timedelta(hours=1, minutes=i)) for i in range(1, 21)]
    awx.lines += [line(100 + i, now - timedelta(hours=2, minutes=i), cur="USD") for i in range(1, 4)]
    awx.lines += [line(200 + i, now - timedelta(hours=2, minutes=i), cur="GBP") for i in range(1, 3)]
    sync_scheduled_transactions(None, "Hourly")
    aud = [bt(f"ft-{i}") for i in range(1, 21)]
    usd = [bt(f"ft-{100 + i}") for i in range(1, 4)]
    check("A1 all 20 AUD lines in, across three pages", all(aud), [i for i, x in enumerate(aud, 1) if not x])
    check("A1 AUD lines on the AUD bank account", all(x and x.bank_account == accts["FT AWX AUD"] for x in aud))
    check("A1 USD lines on the USD bank account", all(x and x.bank_account == accts["FT AWX USD"] for x in usd))
    check("A1 GBP (no row) not inserted", not bt("ft-201"))
    check("A1 submitted", all(x and x.docstatus == 1 for x in aud + usd))
    rep = json.loads(frappe.db.get_single_value("Airwallex Settings", "sync_report"))
    check("A1 report Completed, GBP counted as unrouted",
          rep["status"] == "Completed" and rep["logins"][0]["unrouted"].get("GBP") == 2, rep)
    logins = [c for c in awx.calls if c[1].endswith("authentication/login")]
    first = [c[2].get("page_num") for c in awx.calls if c[1].endswith("/financial_transactions")][:5]
    check("A1 one login, each page read once (not once per client row)",
          len(logins) == 1 and first[:4] == [0, 1, 2, 3], (len(logins), first))
    check("A1 status released", frappe.db.get_single_value("Airwallex Settings", "sync_status") == "Completed")
    cursor1 = frappe.db.get_single_value("Airwallex Settings", "last_sync_date")
    check("A1 next window point moved to the run start", cursor1 and abs((frappe.utils.get_datetime(cursor1) - now).total_seconds()) < 120, cursor1)

    # ---- A2 local date (Melbourne): a line made at 08:00 local is that day, not the day before
    d = bt("ft-1")
    check("A2 line date is the local date", str(d.date) == str((now - timedelta(hours=1, minutes=1)).date()), d.date)

    # ---- A3 failure keeps the others, holds the window, leaves nothing behind
    awx.calls.clear()
    frappe.get_doc({"doctype": "Property Setter", "name": "FT ref reqd", "doctype_or_field": "DocField",
                    "doc_type": "Bank Transaction", "field_name": "reference_number", "property": "reqd",
                    "property_type": "Check", "value": "1"}).insert(ignore_permissions=True)
    frappe.clear_cache(doctype="Bank Transaction")
    frappe.db.commit()
    t_fail = now - timedelta(minutes=20)
    awx.lines.append(line(300, now - timedelta(minutes=30), batch="has-ref"))
    awx.lines.append(line(301, t_fail, batch=""))            # no reference -> refused
    awx.lines.append(line(302, now - timedelta(minutes=10), batch="has-ref"))
    sync_scheduled_transactions(None, "Hourly")
    check("A3 good lines either side of a bad one went in", bt("ft-300") and bt("ft-302"))
    check("A3 the bad line left nothing behind", not bt("ft-301"))
    rep = json.loads(frappe.db.get_single_value("Airwallex Settings", "sync_report"))
    check("A3 run says Completed with Errors and names the line",
          rep["status"] == "Completed with Errors" and rep["failed"][0]["id"] == "ft-301", rep.get("failed"))
    check("A3 reason is the real one", "Reference Number" in (rep["failed"][0].get("reason") or ""), rep["failed"][0])
    check("A3 settings status Completed with Errors",
          frappe.db.get_single_value("Airwallex Settings", "sync_status") == "Completed with Errors")
    cursor2 = frappe.utils.get_datetime(frappe.db.get_single_value("Airwallex Settings", "last_sync_date"))
    check("A3 window held at or before the failed line", cursor2 <= t_fail + timedelta(seconds=1), (cursor2, t_fail))
    check("A3 no Error Log row per line", not frappe.db.exists("Error Log", {"method": ["like", "Txn Error%"], "creation": [">", now]}))

    # ---- A4 next run after the cause is fixed brings the failed line in
    for ps in frappe.get_all("Property Setter", {"doc_type": "Bank Transaction", "field_name": "reference_number"}, pluck="name"):
        frappe.delete_doc("Property Setter", ps, ignore_permissions=True)
    frappe.clear_cache(doctype="Bank Transaction")
    frappe.db.commit()
    sync_scheduled_transactions(None, "Hourly")
    rep = json.loads(frappe.db.get_single_value("Airwallex Settings", "sync_report"))
    check("A4 failed line comes in on the next run", bool(bt("ft-301")), rep.get("failed"))
    check("A4 no duplicates (existing skipped)", rep["created"] == 1, rep["created"])

    # ---- A5 late line inside the look-back window
    awx.lines.append(line(400, now - timedelta(days=5)))
    sync_scheduled_transactions(None, "Hourly")
    check("A5 a line Airwallex lists late (5 days back) is picked up", bool(bt("ft-400")))

    # ---- A6 stale lock taken over, live lock respected
    frappe.db.set_single_value("Airwallex Settings", "sync_status", "In Progress", update_modified=False)
    frappe.db.set_single_value("Airwallex Settings", "sync_started_on", now - timedelta(minutes=10), update_modified=False)
    frappe.db.commit()
    awx.lines.append(line(500, now - timedelta(minutes=5)))
    sync_scheduled_transactions(None, "Hourly")
    check("A6 a live run (10 min old) is left alone", not bt("ft-500"))
    frappe.db.set_single_value("Airwallex Settings", "sync_started_on", now - timedelta(hours=5), update_modified=False)
    frappe.db.commit()
    sync_scheduled_transactions(None, "Hourly")
    check("A6 a dead run (5 h old) is taken over", bool(bt("ft-500")))
    check("A6 lock released", frappe.db.get_single_value("Airwallex Settings", "sync_status") != "In Progress")
    frappe.db.set_single_value("Airwallex Settings", "sync_status", "In Progress", update_modified=False)
    frappe.db.set_single_value("Airwallex Settings", "sync_started_on", None, update_modified=False)
    frappe.db.commit()
    from bank_integration.feed import scheduler

    awx.lines.append(line(501, now - timedelta(minutes=4)))
    scheduler.hourly()
    check("A6 scheduler entry point no longer blocked by a dead In Progress", bool(bt("ft-501")))

    # ---- A7 manual range does not move the scheduled start point
    before = frappe.db.get_single_value("Airwallex Settings", "last_sync_date")
    awx.lines.append(line(600, now - timedelta(days=40)))
    sync_transactions(str(now - timedelta(days=41)), str(now - timedelta(days=39)))
    check("A7 manual range brings its line in", bool(bt("ft-600")))
    check("A7 manual range leaves the scheduled start point alone",
          str(frappe.db.get_single_value("Airwallex Settings", "last_sync_date")) == str(before))

    # ---- A8 cursor-style paging also read to the end
    awx.style = "cursor"
    awx.page_size_cap = 3
    awx.lines += [line(700 + i, now - timedelta(days=50, minutes=i)) for i in range(10)]
    sync_transactions(str(now - timedelta(days=51)), str(now - timedelta(days=49)))
    check("A8 page_after paging read to the end", all(bt(f"ft-{700 + i}") for i in range(10)))
    awx.style = "has_more"
    awx.page_size_cap = None

    # ---- H what is "missing": NEW / ALREADY THERE / UNSURE (FY26 reconciliation rules)
    from datetime import datetime as _dt

    def made_line(ba, day, amount, tid=None, cur="AUD", submit=True, matched=False, cancel=False):
        d = frappe.get_doc({"doctype": "Bank Transaction", "bank_account": ba, "date": day,
                            "withdrawal": -amount if amount < 0 else 0, "deposit": amount if amount > 0 else 0,
                            "currency": cur, "description": "FT existing", "transaction_id": tid})
        d.insert(ignore_permissions=True)
        if submit:
            d.submit()
        if matched:
            frappe.db.set_value("Bank Transaction", d.name, {"allocated_amount": abs(amount), "unallocated_amount": 0,
                                                             "status": "Reconciled"}, update_modified=False)
        if cancel:
            d.cancel()
        return d.name

    def at(day, hour=12):
        return _dt(*map(int, day.split("-")), hour)

    frappe.db.sql("delete from `tabBank Transaction` where description='FT existing' or transaction_id like 'st-%%'")
    AUD, USD = accts["FT AWX AUD"], accts["FT AWX USD"]
    ex = {
        "hand": made_line(AUD, "2026-05-20", -1000.0),                    # statement import, no id
        "other_id": made_line(AUD, "2026-05-21", -279.0, tid="old-feed-1"),  # feed made it under another id
        "blank": made_line(None, "2026-05-22", -268.0),                    # C7 row, no bank account, AUD
        "blank_usd": made_line(None, "2026-05-22", -268.0, cur="USD"),     # same amount, other currency: not a candidate
        "draft": made_line(AUD, "2026-05-23", -101.65, submit=False),      # draft counts
        "two_a": made_line(AUD, "2026-05-26", -108.9), "two_b": made_line(AUD, "2026-05-26", -108.9),
        "spare_a": made_line(AUD, "2026-05-27", -15.0), "spare_b": made_line(AUD, "2026-05-28", -15.0),
        "matched": made_line(AUD, "2026-05-24", 6000.0, matched=True),
        "cancelled": made_line(AUD, "2026-08-16", -279.0, cancel=True),   # cancelled: does not count
        "far": made_line(AUD, "2026-07-20", -137.04),                     # 8 days away: not a candidate
    }
    frappe.db.commit()
    st = [  # (id, date, amount): feed lines with no id in ERPNext
        ("st-hand", "2026-05-20", -1000.0), ("st-other", "2026-05-21", -279.0), ("st-blank", "2026-05-22", -268.0),
        ("st-draft", "2026-05-24", -101.65), ("st-two1", "2026-05-26", -108.9), ("st-two2", "2026-05-26", -108.9),
        ("st-spare", "2026-05-27", -15.0), ("st-matched", "2026-05-24", 6000.0),
        # genuine misses from the statement check
        ("st-fee", "2026-08-16", -279.0), ("st-c1", "2026-07-28", -137.04), ("st-c2", "2026-08-03", -204.9),
        ("st-c3", "2026-08-11", -56.95), ("st-c4", "2026-08-11", -15.1),
    ]
    for tid, day, amt in st:
        awx.lines.append({"id": tid, "created_at": iso(at(day)), "currency": "AUD", "net": amt, "amount": amt,
                          "transaction_type": "ISSUING_CAPTURE", "source_type": "CARD_PURCHASE", "source_id": "s" + tid,
                          "status": "SETTLED", "description": tid, "batch_id": "b"})
    awx.lines.append({"id": "st-gbp", "created_at": iso(at("2026-06-01")), "currency": "GBP", "net": -5.0, "amount": -5.0,
                      "transaction_type": "FEE", "source_type": "FEE", "source_id": "x", "status": "SETTLED",
                      "description": "gbp", "batch_id": "b"})
    from bank_integration.feed.tools import catch_up as _cu, possible_doubles

    res = _cu("airwallex", "2026-05-01", "2026-08-31", dry_run=1, currencies="AUD, USD")
    v = {x["id"]: x for x in res["lines"]}
    def vd(i):
        return v.get(i, {}).get("verdict")
    check("H1 a statement-imported line is ALREADY THERE", vd("st-hand") == "ALREADY THERE" and v["st-hand"]["line"] == ex["hand"], v.get("st-hand"))
    check("H2 a line the feed made under another id is ALREADY THERE", vd("st-other") == "ALREADY THERE" and v["st-other"]["line"] == ex["other_id"])
    check("H3 a blank Bank Account line in the same currency is ALREADY THERE", vd("st-blank") == "ALREADY THERE" and v["st-blank"]["line"] == ex["blank"], v.get("st-blank"))
    check("H4 a draft counts", vd("st-draft") == "ALREADY THERE")
    check("H5 two equal lines, two existing: both ALREADY THERE, one each",
          vd("st-two1") == vd("st-two2") == "ALREADY THERE" and {v["st-two1"]["line"], v["st-two2"]["line"]} == {ex["two_a"], ex["two_b"]})
    check("H6 one line, two existing that could be it: UNSURE", vd("st-spare") == "UNSURE", v.get("st-spare"))
    check("H7 the statement's genuine misses are NEW (fee 279.00, 137.04, 204.90, 56.95, 15.10)",
          all(vd(i) == "NEW" for i in ("st-fee", "st-c1", "st-c2", "st-c3", "st-c4")), {i: vd(i) for i in ("st-fee", "st-c1", "st-c2", "st-c3", "st-c4")})
    check("H8 GBP left out, counted as not set up", res["unrouted"].get("GBP") == 1 and "st-gbp" not in v, res["unrouted"])
    mm = {m["month"]: m for m in res["months"]}
    check("H9 totals by month", mm["2026-05"]["ALREADY THERE"] == 7 and mm["2026-05"]["UNSURE"] == 1
          and mm["2026-08"]["NEW"] == 4 and mm["2026-07"]["NEW"] == 1, res["months"])
    check("H10 Adopt offered only where safe", v["st-hand"]["adopt"] and not v["st-other"]["adopt"]
          and not v["st-blank"]["adopt"] and not v["st-matched"]["adopt"] and not v["st-spare"]["adopt"],
          {i: (v[i]["adopt"], v[i]["why"]) for i in ("st-hand", "st-other", "st-blank", "st-matched", "st-spare")})

    # the read-only check script's rule, run on the same data, gives the same answer
    def script_rule(lines):
        claimed, out = set(), {}
        for tid, day, amt in sorted(lines, key=lambda x: x[1]):
            col = "withdrawal" if amt < 0 else "deposit"
            rows = frappe.db.sql(f"""select name, docstatus from `tabBank Transaction`
                where {col} = %s and (bank_account = %s or (ifnull(bank_account,'') = '' and currency = 'AUD'))
                and date between date_sub(%s, interval 3 day) and date_add(%s, interval 3 day)
                and ifnull(transaction_id,'') not like 'st-%%'
                order by abs(datediff(date, %s)), docstatus""", (abs(amt), AUD, day, day, day), as_dict=True)
            live = [r for r in rows if r.docstatus < 2 and r.name not in claimed]
            if live:
                claimed.add(live[0].name)
                out[tid] = "THERE"
            else:
                out[tid] = "ADD"
        return out
    ref = script_rule(st)
    mine = {i: ("ADD" if vd(i) == "NEW" else "THERE") for i, _, _ in st}
    check("H11 same answer as tm_fy26_awx_catchup_check (ALREADY THERE or UNSURE = THERE, NEW = ADD)",
          ref == mine, {i: (ref[i], mine[i]) for i in ref if ref[i] != mine[i]})

    # apply: NEW only, UNSURE only when ticked, adopt writes the id and nothing else
    from bank_integration.feed.tools import catch_up_job as _job

    before = frappe.db.get_value("Bank Transaction", ex["hand"], ["modified", "status", "date", "withdrawal"], as_dict=True)
    _job("airwallex", "2026-05-01 00:00:00", "2026-09-01 00:00:00", currencies="AUD,USD",
         add=None, add_unsure=[], adopt={"st-hand": ex["hand"], "st-other": ex["other_id"]}, by="check")
    after = frappe.db.get_value("Bank Transaction", ex["hand"], ["modified", "status", "date", "withdrawal", "transaction_id"], as_dict=True)
    check("H12 NEW lines added", all(bt(i) for i in ("st-fee", "st-c1", "st-c2", "st-c3", "st-c4")))
    check("H13 ALREADY THERE and UNSURE not added", not any(bt(i) and bt(i).name not in ex.values()
          for i in ("st-hand", "st-blank", "st-draft", "st-two1", "st-spare")))
    check("H14 adopt writes the id only", after.transaction_id == "st-hand" and after.modified == before.modified
          and after.status == before.status and str(after.date) == str(before.date) and after.withdrawal == before.withdrawal, (before, after))
    check("H15 adopt refused on a line with another id",
          frappe.db.get_value("Bank Transaction", ex["other_id"], "transaction_id") == "old-feed-1")
    rep = json.loads(frappe.db.get_single_value("Airwallex Settings", "sync_report"))
    check("H16 the refusal is reported", any(x["id"] == "st-other" for x in rep.get("adopt_refused") or []), rep.get("adopt_refused"))
    _job("airwallex", "2026-05-01 00:00:00", "2026-09-01 00:00:00", currencies="AUD", add=[], add_unsure=["st-spare"], adopt={}, by="check")
    check("H17 an UNSURE line goes in only when ticked", bool(bt("st-spare")))
    res = _cu("airwallex", "2026-05-01", "2026-08-31", dry_run=1, currencies="AUD")
    check("H18 nothing NEW left, adopted line now in by id",
          res["counts"]["NEW"] == 0 and not any(x["id"] == "st-hand" for x in res["lines"]), res["counts"])
    d = possible_doubles(AUD, "2026-05-01", "2026-05-31")
    check("H19 possible doubles lists a feed line next to an equal line", d["possible_doubles"] >= 1, d)

    # floor: never before Reconciled Up To
    frappe.db.set_single_value("Airwallex Settings", "reconciled_to", "2026-06-30", update_modified=False)
    frappe.db.commit()
    try:
        _cu("airwallex", "2026-05-01", "2026-08-31", dry_run=1)
        refused = False
    except frappe.ValidationError:
        refused = True
    check("H20 a From on or before Reconciled Up To is refused", refused)
    res = _cu("airwallex", None, "2026-08-31", dry_run=1, currencies="AUD")
    check("H21 no From = the day after Reconciled Up To", res["from"].startswith("2026-07-01"), res["from"])
    from bank_integration.feed import common as _c

    check("H22 the scheduled window never starts before it either",
          _c.window_start("2026-07-02 00:00:00", 1, "2026-06-30") >= frappe.utils.get_datetime("2026-07-01"))
    frappe.db.set_single_value("Airwallex Settings", "reconciled_to", None, update_modified=False)
    frappe.db.sql("delete from `tabBank Transaction` where description='FT existing' or transaction_id like 'st-%%'")
    awx.lines = [x for x in awx.lines if not str(x["id"]).startswith("st-")]
    frappe.db.commit()

    # paging when Airwallex caps the page and does not say has_more
    awx.page_size_cap = 5
    awx.no_has_more = True
    awx.lines += [line(1100 + i, now - timedelta(days=20, minutes=i)) for i in range(12)]
    res = _cu("airwallex", str((now - timedelta(days=21)).date()), str((now - timedelta(days=19)).date()), dry_run=1, currencies="AUD")
    check("H23 every page read when Airwallex caps the page and gives no has_more",
          {f"ft-{1100 + i}" for i in range(12)} <= {x["id"] for x in res["lines"]}, len(res["lines"]))
    awx.page_size_cap = None
    awx.no_has_more = False
    awx.lines = [x for x in awx.lines if not (str(x["id"]).startswith("ft-11") and len(x["id"]) == 7)]

    # ---- L logging
    from bank_integration.feed.maintenance import counts, delete_logs_before, purge_job

    awx.lines.append(line(800, now - timedelta(minutes=3)))
    frappe.cache.delete_value("bank_integration:airwallex_token:CLIENT-ONE-LOGIN")
    sync_scheduled_transactions(None, "Hourly")
    new_rows = frappe.get_all("Bank Integration Log", filters={"creation": [">=", now]},
                              fields=["status", "url", "request_headers", "response_data", "message"])
    api_rows = [r for r in new_rows if r.url]
    check("L1 Enable Log off: no rows for successful calls", not [r for r in api_rows if r.status == "Success"],
          [r.url for r in api_rows if r.status == "Success"][:3])
    frappe.db.set_single_value("Airwallex Settings", "enable_log", 1, update_modified=False)
    frappe.cache.delete_value("bank_integration:airwallex_token:CLIENT-ONE-LOGIN")
    frappe.db.commit()
    awx.lines.append(line(801, now - timedelta(minutes=2)))
    sync_scheduled_transactions(None, "Hourly")
    rows = frappe.get_all("Bank Integration Log", filters={"creation": [">=", now]},
                          fields=["status", "url", "request_headers", "response_data", "message"])
    blob = json.dumps(rows)
    check("L2 Enable Log on: calls are logged", any(r.url and r.status == "Success" for r in rows))
    check("L3 no API key in any row", "LONGLIFE-API-KEY-XYZ" not in blob)
    check("L4 no bearer token in any row", "TOKEN-SECRET-123" not in blob)
    check("L5 the 404 lookups are still logged as errors", any(r.status == "Error" for r in rows))
    frappe.db.set_single_value("Airwallex Settings", "enable_log", 0, update_modified=False)

    # ---- T token expiry read from expires_at
    cached = frappe.cache.get_value("bank_integration:airwallex_token:CLIENT-ONE-LOGIN") or {}
    check("T1 token kept in the cache, expiry taken from expires_at",
          cached.get("token") == "TOKEN-SECRET-123" and str(cached.get("expiry", "")).startswith(("2098", "2099")), cached)
    check("T2 no token in any database field",
          "TOKEN-SECRET-123" not in json.dumps(frappe.db.sql("select * from `tabAirwallex Client`"), default=str)
          and "TOKEN-SECRET-123" not in json.dumps(frappe.db.sql("select value from tabSingles where doctype like '%%Settings'"), default=str))

    # ---- P purge
    for i in range(25):
        frappe.get_doc({"doctype": "Bank Integration Log", "status": "Success", "url": "u",
                        "request_headers": "{'x-api-key': 'OLD-KEY'}", "message": "old"}).insert(ignore_permissions=True)
    frappe.get_doc({"doctype": "Error Log", "method": "Txn Error - CLIENT-O", "error": "x"}).insert(ignore_permissions=True)
    frappe.get_doc({"doctype": "Error Log", "method": "Something else", "error": "keep me"}).insert(ignore_permissions=True)
    frappe.db.commit()
    c = counts(frappe.utils.now_datetime() + timedelta(seconds=1))
    check("P1 dry run counts rows and the ones holding keys",
          c["bank_integration_log"]["to_delete"] >= 25 and c["bank_integration_log"]["holding_keys_or_tokens"] >= 25, c)
    gone = delete_logs_before(frappe.utils.now_datetime() + timedelta(seconds=1), batch=7, max_batches=2)
    check("P2 batches of the size asked", gone == 14, gone)
    purge_job(str(frappe.utils.now_datetime() + timedelta(seconds=1)), error_logs=True, by="check")
    left = frappe.db.count("Bank Integration Log")
    check("P3 purge clears the log (only its own summary row left)", left == 1, left)
    check("P4 feed Error Log rows gone, others kept",
          not frappe.db.exists("Error Log", {"method": "Txn Error - CLIENT-O"})
          and frappe.db.exists("Error Log", {"method": "Something else"}))
    from bank_integration.bank_integration.doctype.bank_integration_log.bank_integration_log import BankIntegrationLog

    check("P5 Log Settings can clear it (clear_old_logs)", callable(getattr(BankIntegrationLog, "clear_old_logs", None)))
    ls = frappe.get_doc("Log Settings")
    ls.add_default_logtypes()
    check("P6 Log Settings picks up the 30-day default",
          any(r.ref_doctype == "Bank Integration Log" and r.days == 30 for r in ls.logs_to_clear))

    # ---- C catch-up
    from bank_integration.feed.tools import catch_up, catch_up_job, feed_check

    frappe.db.sql("delete from `tabBank Transaction` where transaction_id in ('ft-900','ft-901','ft-902')")
    awx.lines += [line(900, now - timedelta(days=200)), line(901, now - timedelta(days=150), cur="USD"),
                  line(902, now - timedelta(days=100))]
    frappe.db.commit()
    res = catch_up("airwallex", str((now - timedelta(days=210)).date()), str(now.date()), dry_run=1, currencies="AUD")
    ids = {x["id"] for x in res["lines"] if x["verdict"] == "NEW"}
    check("C1 dry run lists the missing AUD lines only", ids == {"ft-900", "ft-902"}, ids)
    check("C2 dry run writes nothing", not bt("ft-900"))
    catch_up_job("airwallex", str(now - timedelta(days=210)), str(now), currencies="AUD", by="check")
    check("C3 catch-up adds them", bt("ft-900") and bt("ft-902") and not bt("ft-901"))
    res = catch_up("airwallex", str((now - timedelta(days=210)).date()), str(now.date()), dry_run=1, currencies="AUD")
    check("C4 nothing left to add, never twice", res["counts"]["NEW"] == 0, res["counts"])

    # ---- F feed check
    fc = feed_check(txn="ft-900", ask_airwallex=1)
    check("F1 feed check routes both currencies", fc["airwallex_routes"][0]["routes"].keys() >= {"AUD", "USD"}, fc["airwallex_routes"])
    check("F2 feed check finds the line in ERPNext and at Airwallex",
          fc["txn"]["in_erpnext"] and fc["txn"]["would_go_to"] == accts["FT AWX AUD"], fc["txn"])

    # ---- G the Bank Feeds page
    from bank_integration.feed import page as bkf

    ov = bkf.overview()
    check("G1 page lists both providers", [p["key"] for p in ov["providers"]] == ["airwallex", "skript"], ov["providers"])
    aud_row = [a for a in ov["accounts"] if a["bank_account"] == accts["FT AWX AUD"]]
    check("G2 page row per bank account with its last line", aud_row and aud_row[0]["last_date"] and aud_row[0]["currency"] == "AUD", aud_row)
    tc = bkf.test_connection("airwallex")
    check("G3 test connection reads Airwallex", tc["logins"] and tc["logins"][0]["ok"], tc)
    frappe.db.set_single_value("Airwallex Settings", "sync_schedule", "Daily", update_modified=False)
    frappe.db.commit()
    awx.lines.append(line(950, now - timedelta(minutes=1)))
    sync_scheduled_transactions(None, "Hourly")
    check("G4 hourly run skips when set to Daily", not bt("ft-950"))
    frappe.db.set_single_value("Airwallex Settings", "enable_log", 1, update_modified=False)
    frappe.db.commit()
    sync_scheduled_transactions(None, None, force=True)
    frappe.db.set_single_value("Airwallex Settings", "enable_log", 0, update_modified=False)
    check("G5 Sync now runs whatever the schedule", bool(bt("ft-950")))
    frappe.db.set_single_value("Airwallex Settings", "sync_schedule", "Hourly", update_modified=False)
    frappe.db.commit()
    log_rows = frappe.get_all("Bank Integration Log", filters={"creation": [">=", now], "url": ["is", "set"]}, fields=["provider"])
    check("G6 call rows carry their provider", log_rows and all(r.provider == "Airwallex" for r in log_rows),
          {r.provider for r in log_rows})

    # ---- R config problems are words, not crashes
    frappe.db.set_value("Account", frappe.db.get_value("Bank Account", accts["FT AWX USD"], "account"), "account_currency", None)
    s = frappe.get_single("Airwallex Settings")
    from bank_integration.airwallex.transaction import client_routes

    lg, probs = client_routes(s)
    check("R1 a ledger account with no currency is named", any("no Account Currency" in p for p in probs), probs)
    frappe.db.set_value("Account", frappe.db.get_value("Bank Account", accts["FT AWX USD"], "account"), "account_currency", "USD")
    frappe.db.commit()

    # ---- S Skript
    reset_state(accts, with_skript=True)
    sk = FakeSkript()
    sk_base.requests = SimpleNamespace(request=sk.request)
    sk_auth.requests = SimpleNamespace(post=sk.post)
    from bank_integration.skript.skript_transaction import sync_scheduled_transactions_skript

    def sline(n, acc, when, amt=-5.0):
        return {"id": f"ft-s{n}", "accountId": acc, "postingDateTime": when.strftime("%Y-%m-%dT%H:%M:%S+11:00"),
                "amount": amt, "description": f"s {n}", "reference": "r", "type": "PAYMENT", "currency": "AUD"}

    sk.size_cap = 4
    sk.lines = [sline(i, "acc-cba", now - timedelta(hours=3, minutes=i)) for i in range(1, 11)]
    sk.lines += [sline(50, "acc-new", now - timedelta(hours=2))]
    sync_scheduled_transactions_skript(None, "Hourly")
    check("S1 Skript: every line in although each page is full", all(bt(f"ft-s{i}") for i in range(1, 11)),
          ([i for i in range(1, 11) if not bt(f"ft-s{i}")], [c[2].get("filter") for c in sk.calls],
           frappe.db.get_single_value("Skript Settings", "sync_report"),
           frappe.get_all("Error Log", {"creation": [">", now]}, ["method", "error"])[-2:]))
    check("S2 Skript: an unmapped account does not stop the rest", not bt("ft-s50") and bt("ft-s1"))
    # Judo posts late: a line posted 3 days back arrives after CBA's newer ones went in
    sk.lines += [sline(60, "acc-judo", now - timedelta(days=3))]
    sync_scheduled_transactions_skript(None, "Hourly")
    check("S3 Skript: a late-posted Judo line is picked up", bool(bt("ft-s60")))
    hs = made_line(accts["FT CBA"], (now - timedelta(days=1)).date(), -55.5)
    frappe.db.commit()
    sk.lines += [sline(70, "acc-cba", now - timedelta(days=1, hours=2), amt=-55.5)]
    sync_scheduled_transactions_skript(None, "Hourly")
    check("S5 Skript: a card line loaded by hand is not added again", not bt("ft-s70"))
    frappe.db.sql("delete from `tabBank Transaction` where name=%s", hs)
    frappe.db.sql("delete from `tabBank Transaction` where description='FT existing'")
    frappe.db.commit()
    srep = json.loads(frappe.db.get_single_value("Skript Settings", "sync_report"))
    check("S4 Skript report per account", "Judo LOC 5803" in srep["accounts"], list(srep["accounts"]))
    frappe.db.commit()

    # ---- M the split patch, on old-shaped data
    from bank_integration.patches.v1_1 import split_provider_settings as patch
    from frappe.utils.password import get_decrypted_password, set_encrypted_password

    old = "Bank Integration Setting"
    for dt in ("Airwallex Settings", "Skript Settings"):
        frappe.db.sql("delete from tabSingles where doctype=%s and field in ('api_url','enabled','last_sync_date','consumer_id')", dt)
    frappe.db.sql("update `tabAirwallex Client` set parent=%s, parenttype=%s, parentfield='airwallex_clients'", (old, old))
    frappe.db.sql("update `tabSkript Account` set parent=%s, parenttype=%s, parentfield='skript_accounts'", (old, old))
    frappe.db.sql("delete from __Auth where doctype='Skript Settings'")
    set_encrypted_password(old, old, "OLD-SECRET", "skript_client_secret")
    for f, v in (("enable_airwallex", 1), ("api_url", "https://old.test/api/v1"), ("last_sync_date", "2026-11-03 03:00:00"),
                 ("skript_consumer_id", "OLDCONSUMER"), ("enable_skript", 1), ("skript_access_token", "PLAIN")):
        frappe.db.sql("insert into tabSingles (doctype, field, value) values (%s, %s, %s)", (old, f, v))
    patch.execute()
    a = frappe.get_single("Airwallex Settings")
    k = frappe.get_single("Skript Settings")
    check("M1 values moved", a.api_url == "https://old.test/api/v1" and str(a.last_sync_date).startswith("2026-11-03")
          and k.consumer_id == "OLDCONSUMER", (a.api_url, a.last_sync_date, k.consumer_id))
    check("M2 client rows moved with their names and keys",
          len(a.clients) == 2 and all(r.get_password("airwallex_api_key") == "LONGLIFE-API-KEY-XYZ" for r in a.clients))
    check("M3 Skript accounts moved", len(k.accounts) == 3)
    check("M4 Skript secret moved", get_decrypted_password("Skript Settings", "Skript Settings", "client_secret") == "OLD-SECRET")
    check("M5 nothing left on the old record (plain token gone)",
          not frappe.db.sql("select count(*) from tabSingles where doctype=%s", old)[0][0])
    patch.execute()
    check("M6 running it again changes nothing", len(frappe.get_single("Airwallex Settings").clients) == 2)
    frappe.db.commit()


if __name__ == "__main__":
    main()
