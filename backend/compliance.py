"""
Compliance Center — the *tooling* side of GDPR, SOC 2 and ISO 27001.

Certification is an audit of your organisation's processes by an external party; no software gives you a
certificate. What software can do — and what auditors and data-protection officers actually ask to see — is
(a) make the data-subject rights executable and provable, (b) enforce the retention rules you declared,
(c) record the periodic human reviews, and (d) produce *evidence* that controls are operating. That's this
module, and each piece is built to be checked rather than asserted:

  * EXPORT and ERASURE don't use a hand-maintained list of "tables with personal data" — a list like that is
    wrong the day someone adds a table. They introspect the schema: every foreign key that points at the
    person's user row (or at their contact row) is followed automatically, and every text column in the
    workspace is searched for the email. A new feature that references users is covered without anyone
    remembering to update this file.
  * ERASURE anonymises rather than deletes the user row (other records reference it, and business records
    like invoices have legal retention duties), removes credentials and passkeys, locks the account instantly,
    rewrites the email wherever it appears in free text — including inside audit entries and workflow run
    records — and then RESCANS every text column and reports what is still there. The audit log keeps the
    event but loses the identity. A legal hold blocks it.
  * What can't be done automatically is said so: a person's *name* typed into a free-text note can't be
    reliably distinguished from someone else's identical name, so such mentions are reported for human review,
    not rewritten.
  * RETENTION policies are enforced by a background loop, not just recorded; the audit log has a floor (90
    days) so a policy can't be used to erase the evidence of its own purge.
  * CONTROLS are computed live from the database and configuration, mapped to SOC 2 / ISO 27001 / GDPR
    references. Controls the app can't observe (backups, vendor review) are manual attestations that expire.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import String, and_, func, or_, select

import models
from database import SessionLocal

logger = logging.getLogger("slate.compliance")

AUDIT_RETENTION_FLOOR_DAYS = 90
RETENTION_ENTITIES = {"audit_log": {"min_days": AUDIT_RETENTION_FLOOR_DAYS, "label": "Audit log"}, "workflow_runs": {"min_days": 1, "label": "Workflow run history"}}
RETENTION_INTERVAL_SECONDS = float(os.getenv("RETENTION_INTERVAL_SECONDS", "3600"))
ATTESTATION_VALID_DAYS = 365
REVIEW_FRESH_DAYS = 90
BREACH_DEADLINE_HOURS = 72
MAX_MENTIONS = 300

_SECRET_COLUMN = re.compile(r"(hashed_password|totp_secret|reset_token|verification_token|secret|_encrypted|embedding|public_key|credential_id|credential_data)", re.I)


class ComplianceError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def email_hash(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


# ===========================================================================
# Schema introspection
# ===========================================================================

def _tenant_tables() -> list:
    return [t for t in models.Base.metadata.sorted_tables if "tenant_id" in t.c]


def _text_columns(table) -> list:
    return [c for c in table.c if isinstance(c.type, String) and not _SECRET_COLUMN.search(c.name)]


def fk_references(target: str) -> list:
    """Every (table, column) whose foreign key points at `target` ('users.id', 'parties.id')."""
    return [(t, c) for t in models.Base.metadata.sorted_tables for c in t.c if any(fk.target_fullname == target for fk in c.foreign_keys)]


def _jsonable(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return None
    return value


def _rows(db, table, where) -> list:
    out = []
    for row in db.execute(select(table).where(where)).mappings().all():
        out.append({k: _jsonable(v) for k, v in row.items() if not _SECRET_COLUMN.search(k)})
    return out


def _find_subject(db, tenant_id: int, email: str):
    email = email.strip().lower()
    user = db.query(models.User).filter(models.User.tenant_id == tenant_id, func.lower(models.User.email) == email).first()
    parties = db.query(models.Party).filter(models.Party.tenant_id == tenant_id, func.lower(models.Party.primary_email) == email).all()
    return user, parties


def scan_text(db, tenant_id: int, needles: list, exclude: Optional[set] = None, limit: int = MAX_MENTIONS, as_email: bool = False) -> list:
    """Every place in the workspace's tables where any needle appears in a text column. -> [{table, column, row_id, snippet}]
    as_email matches email addresses on word boundaries, so 'dana@x.com' is not "found" inside 'xdana@x.com' — which would
    report another person's address as a leftover of this person's erasure. (Names are plain substrings: a name in prose
    has no boundary rule worth the name.)"""
    needles = [n.lower() for n in needles if n]
    patterns = [_email_pattern(n) for n in needles] if as_email else None
    found = []
    for table in _tenant_tables():
        for col in _text_columns(table):
            cond = or_(*[func.lower(col).contains(n, autoescape=True) for n in needles])
            cols = [table.c.id, col] if "id" in table.c else [col]
            for row in db.execute(select(*cols).where(and_(table.c.tenant_id == tenant_id, cond)).limit(limit)).all():
                row_id, value = (row[0], row[1]) if "id" in table.c else (None, row[0])
                if exclude and (table.name, row_id) in exclude:
                    continue
                if patterns is not None and not any(pt.search(value or "") for pt in patterns):
                    continue
                low = (value or "").lower()
                idx = min((low.find(n) for n in needles if n in low), default=0)
                found.append({"table": table.name, "column": col.name, "row_id": row_id, "snippet": (value or "")[max(0, idx - 40): idx + 80]})
                if len(found) >= limit:
                    return found
    return found


# ===========================================================================
# Export (GDPR Art. 15 / 20)
# ===========================================================================

def export_subject(db, tenant_id: int, email: str) -> dict:
    user, parties = _find_subject(db, tenant_id, email)
    if user is None and not parties:
        raise ComplianceError("No user or contact with that email address exists in this workspace", 404)
    tables, included = {}, set()

    def add(table, rows):
        if rows:
            tables.setdefault(table.name, []).extend(rows)
            included.update((table.name, r.get("id")) for r in rows)

    users_table = models.User.__table__
    if user is not None:
        add(users_table, _rows(db, users_table, users_table.c.id == user.id))
        for table, col in fk_references("users.id"):
            if table.name == "users":
                continue
            where = col == user.id
            if "tenant_id" in table.c:
                where = and_(where, table.c.tenant_id == tenant_id)
            add(table, _rows(db, table, where))
    if parties:
        ids = [p.id for p in parties]
        add(models.Party.__table__, _rows(db, models.Party.__table__, models.Party.__table__.c.id.in_(ids)))
        for table, col in fk_references("parties.id"):
            where = col.in_(ids)
            if "tenant_id" in table.c:
                where = and_(where, table.c.tenant_id == tenant_id)
            add(table, _rows(db, table, where))
    mentions = scan_text(db, tenant_id, [email.strip()], exclude=included, as_email=True)
    return {
        "subject": mask_email(email), "generated_at": datetime.utcnow().isoformat(), "format": "slate-dsar-export/1",
        "found": {"user": user is not None, "contacts": len(parties)},
        "tables": tables, "other_mentions": mentions,
        "notes": ["Credentials, secrets, tokens and vector embeddings are deliberately excluded.",
                  "other_mentions lists free-text places where the email address appears outside the records above.",
                  "Records are included when they reference the person directly (by foreign key); records that merely describe them in prose are only found by the mention scan."],
    }


# ===========================================================================
# Erasure (GDPR Art. 17)
# ===========================================================================

def _email_pattern(email: str):
    return re.compile(r"(?<![\w.+\-])" + re.escape(email) + r"(?![\w\-]|\.[\w])", re.I)


def scrub_email(db, tenant_id: int, email: str, replacement: str) -> list:
    """Rewrites the email wherever it appears in free text. Word-boundary matched, so scrubbing 'ann@x.com' can't
    corrupt 'joann@x.com'. Returns [{table, column, rows}] of what was changed."""
    pattern, changed = _email_pattern(email), []
    for table in _tenant_tables():
        if "id" not in table.c:
            continue
        for col in _text_columns(table):
            rows = db.execute(select(table.c.id, col).where(and_(table.c.tenant_id == tenant_id, func.lower(col).contains(email.lower(), autoescape=True)))).all()
            count = 0
            for row_id, value in rows:
                new = pattern.sub(replacement, value or "")
                if new != value:
                    db.execute(table.update().where(table.c.id == row_id).values({col.name: new}))
                    count += 1
            if count:
                changed.append({"table": table.name, "column": col.name, "rows": count})
    return changed


def erase_subject(db, tenant_id: int, actor: models.User, email: str) -> dict:
    user, parties = _find_subject(db, tenant_id, email)
    if user is None and not parties:
        raise ComplianceError("No user or contact with that email address exists in this workspace", 404)
    hold = db.query(models.LegalHold).filter(models.LegalHold.tenant_id == tenant_id, func.lower(models.LegalHold.subject_email) == email.strip().lower(),
                                             models.LegalHold.released_at.is_(None)).first()
    if hold is not None:
        raise ComplianceError(f"Erasure refused: a legal hold is active for this person ({hold.reason}). Release the hold first.", 409)

    old_email, old_name = email.strip(), (user.name if user else None) or (parties[0].display_name if parties else None)
    replacement = old_email
    report: dict = {"user_anonymised": False, "contacts_anonymised": 0, "passkeys_removed": 0, "audit_entries_pseudonymised": 0}

    if user is not None:
        if user.id == actor.id:
            raise ComplianceError("You can't erase the account you're signed in with — ask another organization admin", 409)
        if user.role == "org_admin":
            locked = {r[0] for r in db.query(models.UserLock.user_id).filter(models.UserLock.tenant_id == tenant_id)}
            admins = [u for u in db.query(models.User).filter(models.User.tenant_id == tenant_id, models.User.role == "org_admin") if u.id not in locked]
            if len(admins) <= 1:
                raise ComplianceError("This is the workspace's last organization admin — promote another admin first", 409)
        from auth import hash_password  # late: auth imports models, and this module is imported by routers
        replacement = f"deleted-user-{user.id}@deleted.invalid"
        user.email, user.name = replacement, "Deleted user"
        user.hashed_password = hash_password(secrets.token_urlsafe(32))
        user.totp_secret, user.mfa_enabled = None, False
        user.verification_token = user.reset_token = user.reset_token_expires = None
        report["passkeys_removed"] = db.query(models.WebAuthnCredential).filter(models.WebAuthnCredential.user_id == user.id).delete(synchronize_session=False)
        if db.query(models.UserLock).filter(models.UserLock.user_id == user.id).first() is None:
            db.add(models.UserLock(tenant_id=tenant_id, user_id=user.id, reason="erased (GDPR Art. 17)", created_by=actor.id))
        # The audit trail keeps the *event*; it loses the identity.
        report["audit_entries_pseudonymised"] = db.query(models.AuditLog).filter(models.AuditLog.tenant_id == tenant_id, models.AuditLog.actor_user_id == user.id).update(
            {"actor_name": "Deleted user"}, synchronize_session=False)
        report["user_anonymised"] = True
    for party in parties:
        party.display_name, party.primary_email = "Deleted contact", None
        replacement = replacement if user is not None else "deleted-contact@deleted.invalid"
    report["contacts_anonymised"] = len(parties)
    db.flush()

    report["scrubbed_free_text"] = scrub_email(db, tenant_id, old_email, replacement)
    db.flush()
    # Verify instead of promising: rescan everything.
    report["residual_email_mentions"] = scan_text(db, tenant_id, [old_email], as_email=True)
    needs_review = scan_text(db, tenant_id, [old_name]) if old_name and len(old_name) >= 5 and " " in old_name else []
    report["needs_review"] = [{k: m[k] for k in ("table", "column", "row_id")} for m in needs_review]
    report["retained"] = ["Business records that reference the person (deals, invoices, tickets) are kept for their own legal retention periods, now linked to an anonymised identity.",
                          "Audit log entries are kept as events with the identity removed."]
    report["clean"] = not report["residual_email_mentions"]
    return report


# ===========================================================================
# Retention
# ===========================================================================

def run_retention(db, tenant_id: int) -> dict:
    now = datetime.utcnow()
    results = {}
    for policy in db.query(models.RetentionPolicy).filter(models.RetentionPolicy.tenant_id == tenant_id, models.RetentionPolicy.enabled == True).all():  # noqa: E712
        cutoff = now - timedelta(days=policy.days)
        deleted = 0
        if policy.entity == "audit_log":
            deleted = db.query(models.AuditLog).filter(models.AuditLog.tenant_id == tenant_id, models.AuditLog.created_at < cutoff).delete(synchronize_session=False)
        elif policy.entity == "workflow_runs":
            ids = [r[0] for r in db.query(models.WorkflowRun.id).filter(models.WorkflowRun.tenant_id == tenant_id, models.WorkflowRun.created_at < cutoff,
                                                                         models.WorkflowRun.status.in_(("succeeded", "failed")))]
            if ids:
                db.query(models.WorkflowNodeRun).filter(models.WorkflowNodeRun.run_id.in_(ids)).delete(synchronize_session=False)
                deleted = db.query(models.WorkflowRun).filter(models.WorkflowRun.id.in_(ids)).delete(synchronize_session=False)
        policy.last_run_at, policy.last_deleted = now, deleted
        results[policy.entity] = deleted
    if sum(results.values()) > 0:
        # Only when something was actually deleted (an hourly "purged nothing" row would just be noise), and written
        # AFTER the purge, so the record of the purge can't be purged by it.
        db.add(models.AuditLog(tenant_id=tenant_id, actor_user_id=None, actor_name="Retention policy", action="retention.purged", resource_type="retention_policy",
                               resource_id=None, after=json.dumps(results)))
    db.commit()
    return results


def _retention_pass() -> None:
    db = SessionLocal()
    try:
        for (tenant_id,) in db.query(models.RetentionPolicy.tenant_id).filter(models.RetentionPolicy.enabled == True).distinct().all():  # noqa: E712
            try:
                run_retention(db, tenant_id)
            except Exception:  # noqa: BLE001
                db.rollback()
                logger.exception("retention run failed for tenant %s", tenant_id)
    finally:
        db.close()


async def retention_loop() -> None:
    while True:
        await asyncio.sleep(RETENTION_INTERVAL_SECONDS)
        try:
            await asyncio.to_thread(_retention_pass)
        except Exception:  # noqa: BLE001
            logger.exception("retention loop failed")


# ===========================================================================
# Controls and evidence
# ===========================================================================

def _ctl(cid, title, frameworks, status, evidence, how):
    return {"id": cid, "title": title, "frameworks": frameworks, "status": status, "evidence": evidence, "how": how}


MANUAL_CONTROLS = {
    "ops.backups": ("Backups are taken and restores are tested", ["SOC2 A1.2", "ISO27001 A.8.13"], "Attest after the last restore test; this app can't observe your database backups."),
    "ops.vendor_review": ("Sub-processors and vendors are reviewed annually", ["SOC2 CC9.2", "ISO27001 A.5.19", "GDPR Art. 28"], "Attest after reviewing the AI/email/hosting providers this workspace sends data to."),
    "dev.change_management": ("Changes are reviewed and tested before release", ["SOC2 CC8.1", "ISO27001 A.8.32"], "Attest that code review and the CI pipeline gate production deploys."),
}


def compute_controls(db, tenant: models.Tenant) -> list:
    tid, now = tenant.id, datetime.utcnow()
    users = db.query(models.User).filter(models.User.tenant_id == tid).all()
    locked = {r[0] for r in db.query(models.UserLock.user_id).filter(models.UserLock.tenant_id == tid)}
    active = [u for u in users if u.id not in locked]
    admins = [u for u in active if u.role == "org_admin"]
    out = []

    no_mfa = [u.email for u in admins if not u.mfa_enabled]
    out.append(_ctl("access.mfa_admins", "Every organization admin has multi-factor authentication", ["SOC2 CC6.1", "ISO27001 A.8.5"], "pass" if admins and not no_mfa else "fail",
                    {"org_admins": len(admins), "without_mfa": len(no_mfa)}, "Measured: users with role org_admin and mfa_enabled."))
    pct = round(100 * sum(1 for u in active if u.mfa_enabled) / len(active)) if active else 0
    out.append(_ctl("access.mfa_coverage", "Multi-factor authentication coverage across all users", ["SOC2 CC6.1", "ISO27001 A.8.5"], "pass" if pct >= 80 else "warn" if pct >= 50 else "fail",
                    {"users": len(active), "mfa_percent": pct}, "Pass at 80% or more, warn at 50%."))
    sso = [name for name, model in (("ldap", models.LdapConfig), ("saml", models.SamlConfig), ("oauth", models.OAuthConfig))
           if db.query(model).filter(model.tenant_id == tid, model.enabled == True).first()]  # noqa: E712
    out.append(_ctl("access.sso", "Centralised sign-in (SSO) is enabled", ["SOC2 CC6.1", "ISO27001 A.5.16"], "pass" if sso else "warn", {"enabled": sso}, "Any of LDAP, SAML or OAuth/OIDC enabled for the workspace."))
    out.append(_ctl("access.admin_count", "Organization admins: more than one, not many", ["SOC2 CC6.3", "ISO27001 A.8.2"], "pass" if 2 <= len(admins) <= 5 else "warn", {"org_admins": len(admins)},
                    "One admin is a single point of failure; more than five is excessive standing privilege."))
    last_review = db.query(models.AccessReview).filter(models.AccessReview.tenant_id == tid, models.AccessReview.status == "completed").order_by(models.AccessReview.completed_at.desc()).first()
    age = (now - last_review.completed_at).days if last_review and last_review.completed_at else None
    out.append(_ctl("access.review", f"User access reviewed in the last {REVIEW_FRESH_DAYS} days", ["SOC2 CC6.2", "ISO27001 A.5.18"], "pass" if age is not None and age <= REVIEW_FRESH_DAYS else "fail",
                    {"last_completed_days_ago": age}, "Run a review under Access reviews; completing one records who decided what."))
    out.append(_ctl("crypto.field_encryption_key", "Field-level encryption uses a deployment-specific key", ["SOC2 CC6.7", "ISO27001 A.8.24"], "pass" if os.getenv("FIELD_ENCRYPTION_KEY") else "fail",
                    {"key_configured": bool(os.getenv("FIELD_ENCRYPTION_KEY"))}, "FIELD_ENCRYPTION_KEY set (not the development default)."))
    base = os.getenv("APP_BASE_URL", "")
    out.append(_ctl("crypto.transport", "The application is served over HTTPS", ["SOC2 CC6.7", "ISO27001 A.8.24"], "pass" if base.startswith("https://") else "warn", {"app_base_url_scheme": base.split(":")[0] or None},
                    "APP_BASE_URL starts with https://. (TLS terminates at your ingress; this reads configuration, not the network.)"))
    recent = db.query(func.count(models.AuditLog.id)).filter(models.AuditLog.tenant_id == tid, models.AuditLog.created_at > now - timedelta(days=30)).scalar()
    out.append(_ctl("logging.audit_active", "Security-relevant actions are being audit-logged", ["SOC2 CC7.2", "ISO27001 A.8.15"], "pass" if recent else "warn", {"entries_last_30_days": recent}, "Audit entries written in the last 30 days."))
    pol = db.query(models.RetentionPolicy).filter(models.RetentionPolicy.tenant_id == tid, models.RetentionPolicy.entity == "audit_log", models.RetentionPolicy.enabled == True).first()  # noqa: E712
    out.append(_ctl("logging.audit_retention", "Audit log retention is at least 12 months (or unlimited)", ["SOC2 CC7.2", "ISO27001 A.8.15"], "pass" if pol is None or pol.days >= 365 else "warn",
                    {"retention_days": pol.days if pol else None}, "No policy keeps everything; a policy under 365 days warns."))
    stale = db.query(func.count(models.DataSubjectRequest.id)).filter(models.DataSubjectRequest.tenant_id == tid, models.DataSubjectRequest.status != "completed",
                                                                       models.DataSubjectRequest.created_at < now - timedelta(days=30)).scalar()
    out.append(_ctl("privacy.dsar_sla", "Data-subject requests are answered within 30 days", ["GDPR Art. 12(3)", "ISO27701 7.3"], "fail" if stale else "pass", {"overdue": stale}, "Requests not completed after 30 days."))
    breaches = db.query(models.BreachIncident).filter(models.BreachIncident.tenant_id == tid, models.BreachIncident.status == "open").all()
    overdue = [b for b in breaches if b.authority_notified_at is None and now > b.discovered_at + timedelta(hours=BREACH_DEADLINE_HOURS)]
    out.append(_ctl("privacy.breach_72h", "Breaches are reported to the authority within 72 hours", ["GDPR Art. 33", "ISO27001 A.5.26"], "fail" if overdue else "warn" if breaches else "pass",
                    {"open": len(breaches), "overdue": len(overdue)}, "Open incidents with no authority notification past 72 hours fail; open within the window warn."))
    consents = db.query(func.count(models.ConsentRecord.id)).filter(models.ConsentRecord.tenant_id == tid).scalar()
    out.append(_ctl("privacy.consent_ledger", "Consent is recorded in an append-only ledger", ["GDPR Art. 7"], "pass" if consents else "warn", {"records": consents}, "At least one consent record exists."))

    attested = {a.control_id: a for a in db.query(models.ControlAttestation).filter(models.ControlAttestation.tenant_id == tid).order_by(models.ControlAttestation.id)}
    for cid, (title, fws, how) in MANUAL_CONTROLS.items():
        a = attested.get(cid)
        fresh = a is not None and (now - a.created_at).days <= ATTESTATION_VALID_DAYS
        out.append(_ctl(cid, title, fws, "attested" if fresh else "manual",
                        {"attested_days_ago": (now - a.created_at).days if a else None, "note": a.note if a and fresh else None}, how))
    return out


def summarise(controls: list) -> dict:
    counts: dict = {}
    for c in controls:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    measured = counts.get("pass", 0) + counts.get("warn", 0) + counts.get("fail", 0)
    return {"counts": counts, "score_percent": round(100 * counts.get("pass", 0) / measured) if measured else None}


def evidence_pack(db, tenant: models.Tenant) -> dict:
    controls = compute_controls(db, tenant)
    tid = tenant.id
    pack = {
        "format": "slate-compliance-evidence/1", "workspace": tenant.name, "generated_at": datetime.utcnow().isoformat(), "controls": controls, "summary": summarise(controls),
        "retention_policies": [{"entity": p.entity, "days": p.days, "enabled": p.enabled, "last_run_at": p.last_run_at.isoformat() if p.last_run_at else None, "last_deleted": p.last_deleted}
                               for p in db.query(models.RetentionPolicy).filter(models.RetentionPolicy.tenant_id == tid)],
        "data_subject_requests": [{"id": d.id, "kind": d.kind, "subject": d.subject_masked, "status": d.status, "completed_at": d.completed_at.isoformat() if d.completed_at else None}
                                  for d in db.query(models.DataSubjectRequest).filter(models.DataSubjectRequest.tenant_id == tid).order_by(models.DataSubjectRequest.id)],
        "access_reviews": [{"id": r.id, "title": r.title, "status": r.status, "completed_at": r.completed_at.isoformat() if r.completed_at else None}
                           for r in db.query(models.AccessReview).filter(models.AccessReview.tenant_id == tid).order_by(models.AccessReview.id)],
        "breaches": [{"id": b.id, "title": b.title, "severity": b.severity, "status": b.status, "discovered_at": b.discovered_at.isoformat(),
                      "authority_notified_at": b.authority_notified_at.isoformat() if b.authority_notified_at else None} for b in db.query(models.BreachIncident).filter(models.BreachIncident.tenant_id == tid)],
        "audit_log": {"entries_total": db.query(func.count(models.AuditLog.id)).filter(models.AuditLog.tenant_id == tid).scalar(),
                      "entries_last_30_days": db.query(func.count(models.AuditLog.id)).filter(models.AuditLog.tenant_id == tid, models.AuditLog.created_at > datetime.utcnow() - timedelta(days=30)).scalar()},
        "suspended_users": db.query(func.count(models.UserLock.id)).filter(models.UserLock.tenant_id == tid).scalar(),
    }
    # Tamper-evidence for the copy that gets emailed to an auditor: the hash is over the canonical JSON of everything above.
    pack["integrity_sha256"] = hashlib.sha256(json.dumps(pack, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    return pack
