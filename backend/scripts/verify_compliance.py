#!/usr/bin/env python3
"""
End-to-end verification of the Compliance Center (backend/compliance.py, routers/compliance.py) and of instant
access revocation.

    python backend/scripts/verify_compliance.py

The point of this script is that it does not take the module's word for anything. After an erasure it scans every
text column of every table in the raw SQLite file for the erased email — independently of the module's own
schema introspection — and checks that a similar address ('xdana@…') was not damaged by the scrub. After a
retention run it counts rows. After a suspension it opens a real WebSocket handshake. See verify_common.py for
the harness contract.
"""

import asyncio
import hashlib
import json
import re
import secrets
import sqlite3
import sys
import time
from datetime import datetime, timedelta

import httpx
import pyotp
from cryptography.fernet import Fernet

import verify_common as v
from verify_common import SLATE_PORT, Tenant, api, check, db_all, db_exec, db_one, section

A = B = None
DANA, XDANA, CONTACT = "dana@example.com", "xdana@example.com", "client@acme.test"
CTX: dict = {}


def ts(days_ago=0, hours_ago=0):
    return (datetime.utcnow() - timedelta(days=days_ago, hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S.%f")


def db_scan(needle: str) -> list:
    """Independent of the module under test: every text column of every table, in the raw database file. Matches the
    address as an address (not as a substring of a longer one: 'xdana@…' does not contain the person 'dana@…')."""
    pattern = re.compile(r"(?<![\w.+\-])" + re.escape(needle) + r"(?![\w\-]|\.[\w])", re.I)
    con = sqlite3.connect(v.DB_PATH)
    found = []
    try:
        for (table,) in con.execute("select name from sqlite_master where type = 'table' and name not like 'sqlite_%'").fetchall():
            for _cid, cname, ctype, *_ in con.execute(f'pragma table_info("{table}")').fetchall():
                if any(k in (ctype or "").upper() for k in ("CHAR", "TEXT", "CLOB")):
                    values = con.execute(f'select "{cname}" from "{table}" where lower("{cname}") like ?', (f"%{needle.lower()}%",)).fetchall()
                    n = sum(1 for (value,) in values if pattern.search(value or ""))
                    if n:
                        found.append((table, cname, n))
    finally:
        con.close()
    return found


def login(email: str, password: str = "teammate-pw-123") -> httpx.Response:
    return api("POST", "/api/auth/login", json={"email": email, "password": password})


def ws_connects(token: str) -> bool:
    import websockets

    async def go():
        try:
            async with websockets.connect(f"ws://127.0.0.1:{SLATE_PORT}/api/ws?token={token}", open_timeout=5) as ws:
                return json.loads(await asyncio.wait_for(ws.recv(), 5)).get("type") == "connected"
        except Exception:  # noqa: BLE001 — a refused handshake is the answer we're looking for
            return False

    return asyncio.run(go())


# ===========================================================================
@section("1. Roles and gating")
def sec_roles():
    standard, _ = A.add_user("standard_user")
    manager, _ = A.add_user("manager")
    paths = (("GET", "/api/compliance/controls"), ("GET", "/api/compliance/evidence-pack"), ("GET", "/api/compliance/dsar"), ("POST", "/api/compliance/dsar/export"),
             ("GET", "/api/compliance/retention"), ("POST", "/api/compliance/access-reviews"), ("GET", "/api/compliance/breaches"), ("POST", "/api/compliance/users/1/suspend"))
    check("standard_user and manager are refused (403) everywhere except consent — it's an org-admin surface",
          all(api(m, p, tok, json={"email": "x@y.z", "title": "t", "reason": "r"} if m == "POST" else None).status_code == 403 for tok in (standard, manager) for m, p in paths))
    check("anonymous → 401", api("GET", "/api/compliance/controls").status_code == 401)
    check("a manager CAN record consent (it's operational, not an admin function); a standard user can't",
          api("POST", "/api/compliance/consent", manager, json={"email": "p@q.r", "purpose": "newsletter", "granted": True}).status_code == 200
          and api("POST", "/api/compliance/consent", standard, json={"email": "p@q.r", "purpose": "newsletter", "granted": True}).status_code == 403)


# ===========================================================================
@section("2. Instant revocation: a suspended user's live token dies on the very next request")
def sec_suspend():
    token, email = A.add_user("standard_user")
    CTX["s_id"] = db_one("select id from users where email = ?", (email,))
    check("setup: the user's token works, password login works, and a WebSocket handshake succeeds",
          api("GET", "/api/auth/me", token).status_code == 200 and login(email).status_code == 200 and ws_connects(token))
    check("a non-admin can't suspend anyone (403)", api("POST", f"/api/compliance/users/{CTX['s_id']}/suspend", token, json={"reason": "x"}).status_code == 403)
    check("a reason is required", A.call("POST", f"/api/compliance/users/{CTX['s_id']}/suspend", json={"reason": " "}).status_code == 400)
    r = A.call("POST", f"/api/compliance/users/{CTX['s_id']}/suspend", json={"reason": "left the company"})
    check("an org admin suspends them", r.status_code == 200 and r.json()["suspended"] is True, r.text)
    check("THE SAME TOKEN, issued before the suspension and not yet expired, is rejected on the next request (401)", api("GET", "/api/auth/me", token).status_code == 401)
    check("…so is every other endpoint it could reach", api("GET", "/api/deals", token).status_code == 401)
    r = login(email)
    check("password login is refused with a clear message (403), not a token that then fails", r.status_code == 403 and "suspended" in r.text, r.text)
    check("a new WebSocket connection with that token is refused at the handshake", ws_connects(token) is False)
    check("workspace B can't suspend or restore A's user (404)", all(B.call("POST", f"/api/compliance/users/{CTX['s_id']}/{p}", json={"reason": "x"}).status_code == 404 for p in ("suspend", "restore")))
    listed = {m["email"]: m for m in A.call("GET", "/api/compliance/users").json()}
    check("the member list shows who is suspended and why (org admins only)", listed[email]["suspended"] is True and listed[email]["suspended_reason"] == "left the company"
          and api("GET", "/api/compliance/users", token).status_code == 401 and B.call("GET", "/api/compliance/users").json() != [] and email not in {m["email"] for m in B.call("GET", "/api/compliance/users").json()})
    check("the suspension was audited with its reason", any(e["action"] == "user.suspended" and "left the company" in str(e) for e in A.call("GET", "/api/audit-log").json()))
    check("restoring brings them back — the original token works again", A.call("POST", f"/api/compliance/users/{CTX['s_id']}/restore").status_code == 200 and api("GET", "/api/auth/me", token).status_code == 200 and login(email).status_code == 200)
    admin_id = db_one("select id from users where email = ?", (A.email,))
    check("you can't suspend yourself (409)", A.call("POST", f"/api/compliance/users/{admin_id}/suspend", json={"reason": "oops"}).status_code == 409)
    admin2, e2 = A.add_user("org_admin")
    a2_id = db_one("select id from users where email = ?", (e2,))
    check("a SECOND admin can suspend the first (an admin's own token dies like anyone's), and can't suspend themself",
          api("POST", f"/api/compliance/users/{admin_id}/suspend", admin2, json={"reason": "rotation"}).status_code == 200 and A.call("GET", "/api/auth/me").status_code == 401
          and api("POST", f"/api/compliance/users/{a2_id}/suspend", admin2, json={"reason": "x"}).status_code == 409)
    api("POST", f"/api/compliance/users/{admin_id}/restore", admin2)
    check("…and restoring them brings the first admin's token back", A.call("GET", "/api/auth/me").status_code == 200)
    CTX["admin2"], CTX["admin2_email"], CTX["admin2_id"] = admin2, e2, a2_id


# ===========================================================================
def prepare_subjects():
    """The people and records the export/erasure sections work on."""
    for email, name, role in ((DANA, "Dana Local", "manager"), (XDANA, "Xdana Other", "standard_user")):
        r = A.call("POST", "/api/auth/invite", json={"name": name, "email": email, "password": "teammate-pw-123", "role": role})
        assert r.status_code == 200, r.text
    CTX["dana_token"] = login(DANA).json()["access_token"]
    CTX["dana_id"] = db_one("select id from users where email = ?", (DANA,))
    deal = api("POST", "/api/deals", CTX["dana_token"], json={"name": "Dana's own deal", "company": "Dana Co", "value": 100, "stage": "Lead"}).json()
    api("PATCH", f"/api/deals/{deal['id']}", CTX["dana_token"], json={"stage": "Qualified"})
    CTX["deal_ids"] = [deal["id"]]
    for name in (f"Call {DANA} about renewal", "Dana Local renewal planning", f"Mention of {XDANA} for the other person"):
        CTX["deal_ids"].append(A.call("POST", "/api/deals", json={"name": name, "company": "Ref Co", "value": 5, "stage": "Lead"}).json()["id"])
    db_exec("insert into audit_log (tenant_id, actor_user_id, actor_name, action, resource_type, after, created_at) values (?, ?, 'Dana Local', 'note.added', 'note', ?, ?)",
            (A.id, CTX["dana_id"], json.dumps({"email": DANA, "text": "added by dana"}), ts()))
    A.set_flag("workflow_engine", True)
    wf = A.call("POST", "/api/workflows", json={"name": "dsar-wf", "definition": {"nodes": [{"id": "trigger", "type": "trigger", "params": {"kind": "manual"}},
                                                                                              {"id": "echo", "type": "set", "params": {"values": {"who": "{{ trigger.contact }}"}}}], "edges": [{"from": "trigger", "to": "echo"}]}}).json()
    run = A.call("POST", f"/api/workflows/{wf['id']}/run", json={"input": {"contact": DANA}, "wait": True}, timeout=60).json()
    CTX["wf_id"], CTX["run_id"] = wf["id"], run["id"]
    cdeal = A.call("POST", "/api/deals", json={"name": "Acme renewal", "company": "Acme Client", "value": 900, "stage": "Won"}).json()
    db_exec("update parties set primary_email = ? where tenant_id = ? and display_name = 'Acme Client'", (CONTACT, A.id))
    CTX["contact_deal"] = cdeal["id"]
    CTX["n_dana_audit"] = db_one("select count(*) from audit_log where actor_user_id = ?", (CTX["dana_id"],))


@section("3. GDPR export (Art. 15/20): found by following the schema, not a list")
def sec_export():
    prepare_subjects()
    r = A.call("POST", "/api/compliance/dsar/export", json={"email": DANA})
    body = r.json()
    check("the export is a JSON download, never cached", r.status_code == 200 and "attachment" in r.headers.get("content-disposition", "") and r.headers.get("cache-control") == "no-store", str(r.headers))
    check("it finds the person (case-insensitively) and masks them in its own header", body["found"] == {"user": True, "contacts": 0} and body["subject"] == "d***@example.com"
          and A.call("POST", "/api/compliance/dsar/export", json={"email": "DANA@Example.COM"}).status_code == 200)
    t = body["tables"]
    check("their profile is there", len(t["users"]) == 1 and t["users"][0]["email"] == DANA and t["users"][0]["role"] == "manager")
    check("records that point at them by foreign key are found automatically: the deal they own", any(d["name"] == "Dana's own deal" for d in t["crm_deals"]))
    check("…and their audit-log entries (actor_user_id → users.id)", len(t["audit_log"]) == CTX["n_dana_audit"] >= 1, f"{len(t.get('audit_log', []))} vs {CTX['n_dana_audit']}")
    forbidden = re.compile(r"hashed_password|totp_secret|reset_token|verification_token")
    check("credentials are never exported (no password hash, TOTP secret, or token column in any table)", not any(forbidden.search(k) for rows in t.values() for row in rows for k in row))
    places = {(m["table"], m["column"]) for m in body["other_mentions"]}
    check("the free-text mention scan finds the email where no foreign key points: a deal NAME, a workflow run's trigger data, its node records",
          {("crm_deals", "name"), ("workflow_runs", "trigger_data"), ("workflow_node_runs", "input")} <= places, str(places))
    check("…and does not repeat rows already exported above", not any(m["table"] == "users" for m in body["other_mentions"]))
    check("an erasure-proof record: the request is stored with a HASH and a masked form — the email itself is nowhere in that table",
          db_one("select count(*) from compliance_dsars where subject_hash = ?", (hashlib.sha256(DANA.encode()).hexdigest(),)) >= 1
          and not [x for x in db_scan(DANA) if x[0] == "compliance_dsars"])
    check("an unknown email → 404, recorded as a refused request", A.call("POST", "/api/compliance/dsar/export", json={"email": "nobody@example.com"}).status_code == 404
          and any(d["status"] == "refused" and d["subject"] == "n***@example.com" for d in A.call("GET", "/api/compliance/dsar").json()))
    check("TENANT ISOLATION: workspace B exporting the same email → 404 (it only searches its own workspace)", B.call("POST", "/api/compliance/dsar/export", json={"email": DANA}).status_code == 404)
    c = A.call("POST", "/api/compliance/dsar/export", json={"email": CONTACT}).json()
    check("a CRM contact is a data subject too: their contact row is found, and so are the deals that reference it (parties.id → crm_deals.party_id)",
          c["found"] == {"user": False, "contacts": 1} and len(c["tables"]["parties"]) == 1 and any(d["name"] == "Acme renewal" for d in c["tables"]["crm_deals"]))


# ===========================================================================
@section("4. GDPR erasure (Art. 17): anonymise, scrub, then RESCAN — and verify independently")
def sec_erasure():
    check("erasure needs an explicit confirm:true (400 otherwise)", A.call("POST", "/api/compliance/dsar/erase", json={"email": DANA}).status_code == 400)
    hold = A.call("POST", "/api/compliance/legal-holds", json={"email": DANA, "reason": "litigation hold #42"}).json()
    r = A.call("POST", "/api/compliance/dsar/erase", json={"email": DANA, "confirm": True})
    check("LEGAL HOLD: erasure is refused (409) while a hold is active, naming the reason", r.status_code == 409 and "litigation hold #42" in r.text, r.text)
    check("…and nothing was touched", db_one("select email from users where id = ?", (CTX["dana_id"],)) == DANA)
    A.call("DELETE", f"/api/compliance/legal-holds/{hold['id']}")
    check("the hold is released with a timestamp, not deleted", db_one("select released_at from legal_holds where id = ?", (hold["id"],)) is not None)
    admin_id = db_one("select id from users where email = ?", (A.email,))
    check("an admin can't erase the account they're signed in with (409) — which also means a workspace can never erase its way to having no admin", A.call("POST", "/api/compliance/dsar/erase", json={"email": A.email, "confirm": True}).status_code == 409)
    check("workspace B can't erase A's people (404)", B.call("POST", "/api/compliance/dsar/erase", json={"email": DANA, "confirm": True}).status_code == 404)

    before = {"deals": db_one("select count(*) from crm_deals where tenant_id = ?", (A.id,)), "audit": db_one("select count(*) from audit_log where actor_user_id = ?", (CTX["dana_id"],)),
              "xdana_deal": db_one("select name from crm_deals where id = ?", (CTX["deal_ids"][3],)), "name_deal": db_one("select name from crm_deals where id = ?", (CTX["deal_ids"][2],)),
              "old_pw": db_one("select hashed_password from users where id = ?", (CTX["dana_id"],))}
    r = A.call("POST", "/api/compliance/dsar/erase", json={"email": DANA, "confirm": True})
    rep = r.json()
    check("erasure completes and its own report says the workspace is clean", r.status_code == 200 and rep["clean"] is True and rep["user_anonymised"] is True, r.text[:400])
    check("the report names where it rewrote free text (deal names, workflow run records, audit entries)",
          {("crm_deals", "name"), ("workflow_runs", "trigger_data"), ("audit_log", "after")} <= {(s["table"], s["column"]) for s in rep["scrubbed_free_text"]}, str(rep["scrubbed_free_text"]))

    check("INDEPENDENT VERIFICATION: scanning every text column of every table in the raw database finds the erased email NOWHERE", db_scan(DANA) == [], str(db_scan(DANA)))
    row = db_all("select email, name, hashed_password, mfa_enabled, totp_secret from users where id = ?", (CTX["dana_id"],))[0]
    check("the account row is anonymised: pseudonymous email, name 'Deleted user', a new unusable password, MFA and TOTP secret gone",
          row[0] == f"deleted-user-{CTX['dana_id']}@deleted.invalid" and row[1] == "Deleted user" and row[2] != before["old_pw"] and not row[3] and row[4] is None, str(row))
    check("a lock was created, so the old token and the old password are dead", api("GET", "/api/auth/me", CTX["dana_token"]).status_code == 401 and login(DANA).status_code == 401)
    check("the AUDIT LOG keeps the events and loses the identity: same row count, actor_user_id kept, actor_name now 'Deleted user'",
          db_one("select count(*) from audit_log where actor_user_id = ?", (CTX["dana_id"],)) == before["audit"] and db_one("select count(*) from audit_log where actor_user_id = ? and actor_name <> 'Deleted user'", (CTX["dana_id"],)) == 0)
    check("business records are retained, now linked to the anonymised identity (deal count unchanged, her deal still exists)",
          db_one("select count(*) from crm_deals where tenant_id = ?", (A.id,)) == before["deals"] and db_one("select count(*) from crm_deals where name = ?", ("Dana's own deal",)) == 1)
    check("WORD-BOUNDARY SAFETY: 'xdana@example.com' (a different person whose address CONTAINS dana@example.com) was not corrupted",
          db_one("select name from crm_deals where id = ?", (CTX["deal_ids"][3],)) == before["xdana_deal"] and db_one("select email from users where email = ?", (XDANA,)) == XDANA)
    check("the scrub left the surrounding text intact ('Call deleted-user-N@deleted.invalid about renewal')", db_one("select name from crm_deals where id = ?", (CTX["deal_ids"][1],)) == f"Call deleted-user-{CTX['dana_id']}@deleted.invalid about renewal")
    check("A NAME in prose can't be told apart from someone else's: it's REPORTED for human review, not silently rewritten",
          db_one("select name from crm_deals where id = ?", (CTX["deal_ids"][2],)) == before["name_deal"] and any(m["table"] == "crm_deals" and m["row_id"] == CTX["deal_ids"][2] for m in rep["needs_review"]), str(rep["needs_review"]))
    check("the request is recorded (hash + masked subject only), with the outcome", any(d["kind"] == "erasure" and d["status"] == "completed" and d["summary"]["clean"] is True for d in A.call("GET", "/api/compliance/dsar").json()))
    check("an erased account can't be restored (409)", A.call("POST", f"/api/compliance/users/{CTX['dana_id']}/restore").status_code == 409)
    check("erasing someone who no longer exists → 404", A.call("POST", "/api/compliance/dsar/erase", json={"email": DANA, "confirm": True}).status_code == 404)

    rc = A.call("POST", "/api/compliance/dsar/erase", json={"email": CONTACT, "confirm": True}).json()
    check("a CRM contact erased: row anonymised, email gone, their deal retained", rc["contacts_anonymised"] == 1 and db_scan(CONTACT) == []
          and db_one("select display_name from parties where id = (select party_id from crm_deals where id = ?)", (CTX["contact_deal"],)) == "Deleted contact"
          and db_one("select count(*) from crm_deals where id = ?", (CTX["contact_deal"],)) == 1)




# ===========================================================================
@section("5. Retention: enforced by a background loop, with a floor so a policy can't erase its own evidence")
def sec_retention():
    check("the audit log has a 90-day floor (400 with the reason); workflow-run retention can't be 0 days; unknown entities are refused",
          "floor" in A.call("PUT", "/api/compliance/retention", json={"entity": "audit_log", "days": 30}).text and A.call("PUT", "/api/compliance/retention", json={"entity": "workflow_runs", "days": 0}).status_code == 400
          and A.call("PUT", "/api/compliance/retention", json={"entity": "invoices", "days": 400}).status_code == 400)
    for t, label in ((A, "A"), (B, "B")):
        t.set_flag("workflow_engine", True)
    wf_b = B.call("POST", "/api/workflows", json={"name": "b-wf", "definition": {"nodes": [{"id": "trigger", "type": "trigger", "params": {"kind": "manual"}}], "edges": []}}).json()
    wf_a = CTX["wf_id"]

    def insert_run(tenant, wf, status, days_ago):
        db_exec("insert into workflow_runs (tenant_id, workflow_id, workflow_version, status, trigger_type, definition, created_at) values (?, ?, 1, ?, 'manual', '{}', ?)", (tenant.id, wf, status, ts(days_ago)))
        rid = db_one("select max(id) from workflow_runs")
        db_exec("insert into workflow_node_runs (run_id, tenant_id, node_id, node_type, status, attempts) values (?, ?, 'trigger', 'trigger', ?, 1)", (rid, tenant.id, status))
        return rid

    old_ok, old_fail, old_running, recent = (insert_run(A, wf_a, "succeeded", 5), insert_run(A, wf_a, "failed", 9), insert_run(A, wf_a, "running", 6), insert_run(A, wf_a, "succeeded", 0))
    b_old = insert_run(B, wf_b["id"], "succeeded", 30)
    check("setup: set workflow-run retention to 1 day; policies list shows it", A.call("PUT", "/api/compliance/retention", json={"entity": "workflow_runs", "days": 1}).status_code == 200
          and next(p for p in A.call("GET", "/api/compliance/retention").json() if p["entity"] == "workflow_runs")["days"] == 1)
    r = A.call("POST", "/api/compliance/retention/run").json()
    left = {rid: db_one("select count(*) from workflow_runs where id = ?", (rid,)) for rid in (old_ok, old_fail, old_running, recent, b_old)}
    check("a retention pass deletes runs older than the period — the old succeeded and old failed run — and only those (state checked in the DB: the background loop may have got there first)",
          left[old_ok] == 0 and left[old_fail] == 0 and left[recent] == 1, str((r, left)))
    check("…a run that is still 'running' is never purged, however old", left[old_running] == 1)
    check("…their node records went with them (no orphans)", db_one("select count(*) from workflow_node_runs where run_id in (?, ?)", (old_ok, old_fail)) == 0)
    check("TENANT ISOLATION: workspace B's old run is untouched by A's policy", left[b_old] == 1)
    pol = next(p for p in A.call("GET", "/api/compliance/retention").json() if p["entity"] == "workflow_runs")
    check("the policy reports when it last ran", pol["last_run_at"] is not None)

    # the background loop (RETENTION_INTERVAL_SECONDS=1 here), with no call to /run
    late = insert_run(A, wf_a, "succeeded", 3)
    gone = False
    for _ in range(20):
        if db_one("select count(*) from workflow_runs where id = ?", (late,)) == 0:
            gone = True
            break
        time.sleep(0.5)
    check("ENFORCED, NOT JUST RECORDED: a run that ages past the policy is purged by the background loop with nobody calling anything", gone)

    # audit log
    A.call("PUT", "/api/compliance/retention", json={"entity": "audit_log", "days": 90})
    for days in (200, 120):
        db_exec("insert into audit_log (tenant_id, actor_name, action, resource_type, created_at) values (?, 'old', 'old.event', 'x', ?)", (A.id, ts(days)))
    db_exec("insert into audit_log (tenant_id, actor_name, action, resource_type, created_at) values (?, 'recent', 'recent.event', 'x', ?)", (A.id, ts(10)))
    r = A.call("POST", "/api/compliance/retention/run").json()
    check("audit retention (90 days): rows older than that are purged, recent ones kept",
          db_one("select count(*) from audit_log where tenant_id = ? and action = 'old.event'", (A.id,)) == 0 and db_one("select count(*) from audit_log where tenant_id = ? and action = 'recent.event'", (A.id,)) == 1, str(r))
    purge = [e for e in A.call("GET", "/api/audit-log").json() if e["action"] == "retention.purged"]
    check("the PURGE ITSELF is audited and survives its own run (written after the delete), with the counts", bool(purge) and any("audit_log" in str(e) or "workflow_runs" in str(e) for e in purge), str(purge[:2]))
    time.sleep(3)
    noise = db_one("select count(*) from audit_log where tenant_id = ? and action = 'retention.purged'", (A.id,))
    time.sleep(3)
    check("…and an idle pass writes NOTHING (the loop runs every second here; if it logged empty passes the audit log would flood)",
          db_one("select count(*) from audit_log where tenant_id = ? and action = 'retention.purged'", (A.id,)) == noise)


# ===========================================================================
@section("6. Consent ledger: append-only")
def sec_consent():
    e = "subject@example.org"
    A.call("POST", "/api/compliance/consent", json={"email": e, "purpose": "marketing", "granted": True, "source": "signup form v3"})
    A.call("POST", "/api/compliance/consent", json={"email": e, "purpose": "analytics", "granted": True})
    A.call("POST", "/api/compliance/consent", json={"email": e, "purpose": "marketing", "granted": False, "source": "unsubscribe link"})
    h = A.call("GET", "/api/compliance/consent", params={"email": e.upper()}).json()
    check("the current state is the latest entry per purpose (marketing withdrawn, analytics granted)", h["current"] == {"marketing": False, "analytics": True}, str(h))
    check("…and the whole history is kept, in order, with where each was recorded",
          [(x["purpose"], x["granted"]) for x in h["history"]] == [("marketing", True), ("analytics", True), ("marketing", False)] and h["history"][0]["source"] == "signup form v3")
    check("there is no way to edit or delete a consent record (no such endpoints: 405/404)", A.call("DELETE", "/api/compliance/consent").status_code in (404, 405) and A.call("PUT", "/api/compliance/consent", json={}).status_code in (404, 405))
    check("workspace B sees none of A's ledger", B.call("GET", "/api/compliance/consent", params={"email": e}).json()["history"] == [])
    check("validation: a real email and a purpose are required", A.call("POST", "/api/compliance/consent", json={"email": "nope", "purpose": "x", "granted": True}).status_code == 400)


# ===========================================================================
@section("7. Access reviews: snapshot, decide (not your own), act on the decisions, complete")
def sec_reviews():
    admin2, e2, a2_id = CTX["admin2"], CTX["admin2_email"], CTX["admin2_id"]
    s_tok, s_email = A.add_user("standard_user")
    m_tok, m_email = A.add_user("manager")
    ids = {e: db_one("select id from users where email = ?", (e,)) for e in (s_email, m_email, A.email)}
    r = A.call("POST", "/api/compliance/access-reviews", json={"title": "Q4 review"}).json()
    items = {i["email"]: i for i in r["items"]}
    check("starting a review snapshots every ACTIVE user with their role and MFA state (erased/suspended accounts are not in it)",
          {A.email, e2, s_email, m_email, XDANA} <= set(items) and DANA not in items and not any("deleted.invalid" in e for e in items) and items[m_email]["role_at_review"] == "manager", str(sorted(items)))
    check("a second open review is refused (409)", A.call("POST", "/api/compliance/access-reviews", json={"title": "again"}).status_code == 409)
    rid = r["id"]

    def decide(tenant_or_token, email, decision, new_role=None):
        tok = tenant_or_token if isinstance(tenant_or_token, str) else tenant_or_token.token
        return api("POST", f"/api/compliance/access-reviews/{rid}/items/{items[email]['id']}", tok, json={"decision": decision, "new_role": new_role})

    check("CONFLICT OF INTEREST: you can't decide your own access (403)", decide(A, A.email, "keep").status_code == 403)
    check("…but another org admin can", decide(admin2, A.email, "keep").status_code == 200)
    check("bad decisions are refused: unknown verb, change_role without a valid role", decide(A, m_email, "maybe").status_code == 400 and decide(A, m_email, "change_role", "emperor").status_code == 400)
    check("completing with decisions still pending is refused, saying how many (409)", "pending" in A.call("POST", f"/api/compliance/access-reviews/{rid}/complete").text)
    check("change_role really changes the user's role", decide(A, m_email, "change_role", "standard_user").status_code == 200 and db_one("select role from users where id = ?", (ids[m_email],)) == "standard_user")
    check("revoke really revokes: the user's live token stops working immediately", decide(A, s_email, "revoke").status_code == 200 and api("GET", "/api/auth/me", s_tok).status_code == 401)
    check("every decision that changed access was audited", {"user.role_changed_by_review", "user.access_revoked_by_review"} <= {e["action"] for e in A.call("GET", "/api/audit-log").json()})
    for email in items:
        if email not in (A.email, m_email, s_email):
            decide(admin2 if email != e2 else A, email, "keep")
    done = A.call("POST", f"/api/compliance/access-reviews/{rid}/complete")
    check("with every item decided the review completes, and records totals", done.status_code == 200 and done.json()["status"] == "completed" and done.json()["decided"] == done.json()["total"], done.text)
    check("a completed review can't be changed (409)", decide(A, XDANA, "keep").status_code == 409)
    check("workspace B can't see it (404)", B.call("GET", f"/api/compliance/access-reviews/{rid}").status_code == 404)


# ===========================================================================
@section("8. Breach register and the 72-hour clock (GDPR Art. 33)")
def sec_breach():
    old = (datetime.utcnow() - timedelta(hours=100)).isoformat()
    b = A.call("POST", "/api/compliance/breaches", json={"title": "Laptop with customer export stolen", "severity": "high", "affected_count": 240, "discovered_at": old}).json()
    check("an incident discovered 100 hours ago with no authority notification is OVERDUE (negative hours remaining)", b["overdue"] is True and b["hours_remaining"] < 0, str(b))
    ctl = {c["id"]: c for c in A.call("GET", "/api/compliance/controls").json()["controls"]}
    check("…and the live control 'breaches reported within 72 hours' FAILS because of it", ctl["privacy.breach_72h"]["status"] == "fail" and ctl["privacy.breach_72h"]["evidence"]["overdue"] == 1)
    r = A.call("PATCH", f"/api/compliance/breaches/{b['id']}", json={"authority_notified": True}).json()
    check("recording the notification clears the overdue flag and stamps the time", r["overdue"] is False and r["authority_notified_at"])
    ctl = {c["id"]: c for c in A.call("GET", "/api/compliance/controls").json()["controls"]}
    check("…the control drops to 'warn' while the incident is still open", ctl["privacy.breach_72h"]["status"] == "warn")
    A.call("PATCH", f"/api/compliance/breaches/{b['id']}", json={"status": "closed"})
    ctl = {c["id"]: c for c in A.call("GET", "/api/compliance/controls").json()["controls"]}
    check("…and to 'pass' once it's closed", ctl["privacy.breach_72h"]["status"] == "pass")
    fresh = A.call("POST", "/api/compliance/breaches", json={"title": "Misdirected email", "severity": "low"}).json()
    check("a new incident gets a 72-hour deadline", 71 < fresh["hours_remaining"] <= 72 and fresh["overdue"] is False)
    A.call("PATCH", f"/api/compliance/breaches/{fresh['id']}", json={"status": "closed", "authority_notified": True})
    check("validation: severity must be low/medium/high; bad timestamps are 400", A.call("POST", "/api/compliance/breaches", json={"title": "x", "severity": "apocalyptic"}).status_code == 400
          and A.call("POST", "/api/compliance/breaches", json={"title": "x", "discovered_at": "yesterday-ish"}).status_code == 400)
    check("workspace B can't see or update A's incidents", B.call("GET", "/api/compliance/breaches").json() == [] and B.call("PATCH", f"/api/compliance/breaches/{b['id']}", json={"status": "open"}).status_code == 404)


# ===========================================================================
@section("9. Live controls mapped to SOC 2 / ISO 27001 / GDPR, and a tamper-evident evidence pack")
def sec_controls():
    def controls():
        return {c["id"]: c for c in A.call("GET", "/api/compliance/controls").json()["controls"]}

    c = controls()
    check("every control carries its framework references, its evidence, and how it's measured",
          all(x["frameworks"] and x["how"] and isinstance(x["evidence"], dict) for x in c.values()) and "SOC2 CC6.1" in c["access.mfa_admins"]["frameworks"] and "GDPR Art. 33" in c["privacy.breach_72h"]["frameworks"])
    check("MEASURED, not declared: admins without MFA → the control fails, and its evidence says how many", c["access.mfa_admins"]["status"] == "fail" and c["access.mfa_admins"]["evidence"]["without_mfa"] >= 1)
    for tok in (A.token, CTX["admin2"]):
        secret = api("POST", "/api/auth/mfa/setup", tok).json()["secret"]
        api("POST", "/api/auth/mfa/confirm", tok, json={"code": pyotp.TOTP(secret).now()})
    c = controls()
    check("…every admin enables MFA → the same control now passes", c["access.mfa_admins"]["status"] == "pass")
    active = db_one("select count(*) from users where tenant_id = ? and id not in (select user_id from user_locks)", (A.id,))
    with_mfa = db_one("select count(*) from users where tenant_id = ? and mfa_enabled = 1 and id not in (select user_id from user_locks)", (A.id,))
    check("MFA coverage is the real percentage from the database (and locked/erased users aren't counted)", c["access.mfa_coverage"]["evidence"] == {"users": active, "mfa_percent": round(100 * with_mfa / active)}, str(c["access.mfa_coverage"]))
    check("two admins → the admin-count control passes; a completed review → the review control passes", c["access.admin_count"]["status"] == "pass" and c["access.review"]["status"] == "pass")
    check("SSO is 'warn' until one is enabled…", c["access.sso"]["status"] == "warn")
    A.call("PUT", "/api/auth/oauth-config", json={"provider_name": "IdP", "client_id": "c", "client_secret": "s", "authorization_endpoint": "http://127.0.0.1:1/a", "token_endpoint": "http://127.0.0.1:1/t",
                                                 "jwks_uri": "http://127.0.0.1:1/j", "issuer": "http://127.0.0.1:1", "scopes": "openid email", "enabled": True})
    check("…and passes once OAuth is enabled, naming which", controls()["access.sso"]["status"] == "pass" and controls()["access.sso"]["evidence"]["enabled"] == ["oauth"])
    check("field encryption key configured → pass; plain-http base URL → warn (honestly: it reads configuration, not the network)", c["crypto.field_encryption_key"]["status"] == "pass" and c["crypto.transport"]["status"] == "warn")
    check("audit logging is live; a 90-day audit retention policy warns (<12 months), 365 passes",
          c["logging.audit_active"]["status"] == "pass" and c["logging.audit_retention"]["status"] == "warn"
          and A.call("PUT", "/api/compliance/retention", json={"entity": "audit_log", "days": 365}).status_code == 200 and controls()["logging.audit_retention"]["status"] == "pass")
    check("DSAR timeliness and the consent ledger are evaluated from the data", c["privacy.dsar_sla"]["status"] == "pass" and c["privacy.consent_ledger"]["status"] == "pass")

    check("MANUAL controls (backups, vendor review, change management) start as 'manual' — the app can't see your backups", all(controls()[k]["status"] == "manual" for k in ("ops.backups", "ops.vendor_review", "dev.change_management")))
    check("a throwaway attestation is refused (400); a measured control can't be 'attested' into passing (400)",
          A.call("POST", "/api/compliance/controls/ops.backups/attest", json={"note": "ok"}).status_code == 400 and A.call("POST", "/api/compliance/controls/access.mfa_admins/attest", json={"note": "we promise it is fine"}).status_code == 400)
    check("a real attestation flips it to 'attested', showing the note and its age",
          A.call("POST", "/api/compliance/controls/ops.backups/attest", json={"note": "Restore test of the 3rd of the month passed, 14 minutes"}).status_code == 200
          and controls()["ops.backups"]["status"] == "attested" and "Restore test" in controls()["ops.backups"]["evidence"]["note"])
    db_exec("update control_attestations set created_at = ? where control_id = 'ops.backups'", (ts(400),))
    check("…and an attestation EXPIRES after 365 days: the control reverts to 'manual'", controls()["ops.backups"]["status"] == "manual")

    full = A.call("GET", "/api/compliance/controls").json()
    counts = {}
    for x in full["controls"]:
        counts[x["status"]] = counts.get(x["status"], 0) + 1
    measured = counts.get("pass", 0) + counts.get("warn", 0) + counts.get("fail", 0)
    check("the summary counts and score are consistent with the list", full["summary"]["counts"] == counts and full["summary"]["score_percent"] == round(100 * counts.get("pass", 0) / measured))

    r = A.call("GET", "/api/compliance/evidence-pack")
    pack = r.json()
    claimed = pack.pop("integrity_sha256")
    check("the evidence pack is a download that carries a SHA-256 over its canonical JSON — recomputed independently here, it matches",
          "attachment" in r.headers.get("content-disposition", "") and claimed == hashlib.sha256(json.dumps(pack, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest())
    pack["controls"][0]["status"] = "pass" if pack["controls"][0]["status"] != "pass" else "fail"
    check("…and altering any field breaks the hash (tamper-evident)", claimed != hashlib.sha256(json.dumps(pack, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest())
    text = json.dumps(pack)
    check("the pack contains the controls, DSARs (masked), reviews, breaches and audit statistics — and NO personal email addresses", DANA not in text and CONTACT not in text and "d***@example.com" in text
          and pack["access_reviews"] and pack["breaches"] and pack["audit_log"]["entries_total"] > 0 and any(d["kind"] == "erasure" for d in pack["data_subject_requests"]))
    check("generating the pack is itself audited, with its hash", any(e["action"] == "compliance.evidence_pack_generated" and claimed in str(e) for e in A.call("GET", "/api/audit-log").json()))
    check("workspace B's pack is its own (no leakage of A's reviews or incidents)", not B.call("GET", "/api/compliance/evidence-pack").json()["access_reviews"] and not B.call("GET", "/api/compliance/evidence-pack").json()["breaches"])


def setup():
    global A, B
    v.start_redis()
    v.start_slate(env={"RETENTION_INTERVAL_SECONDS": "1", "FIELD_ENCRYPTION_KEY": Fernet.generate_key().decode()})
    A = Tenant("Verify Compliance A", f"admin-a-{secrets.token_hex(3)}@example.com")
    B = Tenant("Verify Compliance B", f"admin-b-{secrets.token_hex(3)}@example.com")


if __name__ == "__main__":
    sys.exit(v.run("Compliance Center + instant revocation", setup))
