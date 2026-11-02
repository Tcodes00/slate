"""
SQLAlchemy models.

Follows the shared-core pattern from the architecture doc: Tenant, User, and
Party are the platform-wide core. Deal and Activity are CRM's own extension
tables, referencing Party rather than duplicating contact/company data.
Every tenant-scoped table carries tenant_id — multi-tenancy is enforced at
the query layer in every router (see routers/deals.py for the pattern).
"""

from datetime import datetime
from sqlalchemy import Boolean, Column, Integer, String, Float, DateTime, ForeignKey, UniqueConstraint, LargeBinary
from sqlalchemy.orm import relationship

from database import Base


class Tenant(Base):
    __tablename__ = "tenants"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    slug = Column(String, unique=True, nullable=False, index=True)
    preferred_model = Column(String, nullable=True)  # AI Studio setting; falls back to COPILOT_MODEL env var if unset
    created_at = Column(DateTime, default=datetime.utcnow)

    users = relationship("User", back_populates="tenant")


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    email = Column(String, unique=True, nullable=False, index=True)
    hashed_password = Column(String, nullable=False)
    name = Column(String, nullable=False)
    role = Column(String, default="org_admin")  # org_admin, manager, standard_user, read_only
    created_at = Column(DateTime, default=datetime.utcnow)

    email_verified = Column(Boolean, default=False)
    verification_token = Column(String, nullable=True)
    reset_token = Column(String, nullable=True)
    reset_token_expires = Column(DateTime, nullable=True)

    totp_secret = Column(String, nullable=True)
    mfa_enabled = Column(Boolean, default=False)
    employee_id = Column(Integer, ForeignKey("hrm_employees.id"), nullable=True)  # links a login to its HRM record, for ABAC

    tenant = relationship("Tenant", back_populates="users")


class Party(Base):
    """Shared contact/company record — the one table CRM, and later Sales,
    Support, and Marketing, all read and write instead of each keeping their
    own separate 'customer' table."""
    __tablename__ = "parties"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    party_type = Column(String, default="organization")  # person | organization
    display_name = Column(String, nullable=False)
    primary_email = Column(String, nullable=True)
    owner_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    deals = relationship("Deal", back_populates="party")


class Deal(Base):
    __tablename__ = "crm_deals"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    party_id = Column(Integer, ForeignKey("parties.id"), nullable=False)
    name = Column(String, nullable=False)
    stage = Column(String, default="Lead")  # Lead, Qualified, Proposal, Won, Lost
    value = Column(Float, default=0)
    owner_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    party = relationship("Party", back_populates="deals")
    activities = relationship("Activity", back_populates="deal", cascade="all, delete-orphan")


class Activity(Base):
    __tablename__ = "crm_activities"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    deal_id = Column(Integer, ForeignKey("crm_deals.id"), nullable=False)
    content = Column(String, nullable=False)
    occurred_at = Column(DateTime, default=datetime.utcnow)

    deal = relationship("Deal", back_populates="activities")


class DealStageHistory(Base):
    """Every stage transition, logged as it happens — this is what makes a
    real conversion funnel possible. Without it, 'analytics' can only ever
    describe the current moment, never how deals actually moved."""
    __tablename__ = "crm_deal_stage_history"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    deal_id = Column(Integer, ForeignKey("crm_deals.id"), nullable=False)
    from_stage = Column(String, nullable=True)
    to_stage = Column(String, nullable=False)
    changed_at = Column(DateTime, default=datetime.utcnow)


class Invoice(Base):
    """Finance's own extension table — references Party and, optionally, the
    Deal it was generated from, instead of duplicating contact/company data."""
    __tablename__ = "finance_invoices"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    party_id = Column(Integer, ForeignKey("parties.id"), nullable=False)
    deal_id = Column(Integer, ForeignKey("crm_deals.id"), nullable=True)
    invoice_number = Column(String, nullable=False)
    description = Column(String, nullable=False)
    amount = Column(Float, nullable=False)
    status = Column(String, default="Draft")  # Draft, Sent, Paid
    due_date = Column(DateTime, nullable=True)
    issued_at = Column(DateTime, default=datetime.utcnow)
    paid_at = Column(DateTime, nullable=True)

    party = relationship("Party")
    deal = relationship("Deal")


class InventoryItem(Base):
    __tablename__ = "inv_items"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    sku = Column(String, nullable=False)
    name = Column(String, nullable=False)
    unit_of_measure = Column(String, default="unit")
    quantity_on_hand = Column(Float, default=0)
    reorder_point = Column(Float, default=0)
    unit_cost = Column(Float, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class InventoryMovement(Base):
    """An append-only ledger, not just a mutable quantity field — every
    change to stock is a row here, so 'why is this number what it is' is
    always answerable."""
    __tablename__ = "inv_movements"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    item_id = Column(Integer, ForeignKey("inv_items.id"), nullable=False)
    movement_type = Column(String, nullable=False)  # in, out, adjustment
    quantity = Column(Float, nullable=False)
    note = Column(String, nullable=True)
    occurred_at = Column(DateTime, default=datetime.utcnow)


class Quote(Base):
    """Distinct from a CRM Deal on purpose: the Deal is the relationship
    and pipeline; the Quote is the specific priced offer sent to a buyer."""
    __tablename__ = "sales_quotes"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    deal_id = Column(Integer, ForeignKey("crm_deals.id"), nullable=True)
    party_id = Column(Integer, ForeignKey("parties.id"), nullable=False)
    quote_number = Column(String, nullable=False)
    description = Column(String, nullable=False)
    amount = Column(Float, nullable=False)
    status = Column(String, default="Draft")  # Draft, Sent, Accepted, Declined
    created_at = Column(DateTime, default=datetime.utcnow)

    party = relationship("Party")
    deal = relationship("Deal")


class Order(Base):
    __tablename__ = "sales_orders"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    quote_id = Column(Integer, ForeignKey("sales_quotes.id"), nullable=True)
    party_id = Column(Integer, ForeignKey("parties.id"), nullable=False)
    order_number = Column(String, nullable=False)
    description = Column(String, nullable=False)
    amount = Column(Float, nullable=False)
    status = Column(String, default="Pending")  # Pending, Fulfilled, Cancelled
    created_at = Column(DateTime, default=datetime.utcnow)

    party = relationship("Party")


class Employee(Base):
    """An employee is a Party (party_type='person') with HR-specific
    attributes attached — not a second, disconnected 'person' table."""
    __tablename__ = "hrm_employees"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    party_id = Column(Integer, ForeignKey("parties.id"), nullable=False)
    employee_number = Column(String, nullable=False)
    department = Column(String, nullable=True)
    job_title = Column(String, nullable=True)
    manager_id = Column(Integer, ForeignKey("hrm_employees.id"), nullable=True)
    hire_date = Column(DateTime, nullable=True)
    status = Column(String, default="Active")  # Active, On Leave, Departed
    created_at = Column(DateTime, default=datetime.utcnow)

    party = relationship("Party")


class LeaveRequest(Base):
    __tablename__ = "hrm_leave_requests"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    employee_id = Column(Integer, ForeignKey("hrm_employees.id"), nullable=False)
    leave_type = Column(String, default="PTO")  # PTO, Sick, Other
    start_date = Column(DateTime, nullable=False)
    end_date = Column(DateTime, nullable=False)
    status = Column(String, default="Pending")  # Pending, Approved, Denied
    reason = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Ticket(Base):
    __tablename__ = "support_tickets"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    party_id = Column(Integer, ForeignKey("parties.id"), nullable=True)
    subject = Column(String, nullable=False)
    status = Column(String, default="Open")  # Open, In Progress, Resolved, Closed
    priority = Column(String, default="Normal")  # Low, Normal, High, Urgent
    assigned_to_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    party = relationship("Party")


class TicketMessage(Base):
    __tablename__ = "support_messages"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    ticket_id = Column(Integer, ForeignKey("support_tickets.id"), nullable=False)
    sender = Column(String, nullable=False)
    content = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class Vendor(Base):
    __tablename__ = "erp_vendors"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    party_id = Column(Integer, ForeignKey("parties.id"), nullable=False)
    payment_terms = Column(String, default="Net 30")
    rating = Column(Float, default=0)
    bank_details_encrypted = Column(String, nullable=True)  # Fernet-encrypted at rest, see security.py

    party = relationship("Party")


class AuditLog(Base):
    """Append-only. Nothing in this codebase updates or deletes a row
    here — that's what makes it useful as an audit trail rather than just
    another mutable table. Two deliberate, narrow exceptions, both in
    compliance.py and both leaving their own audit entry: GDPR erasure
    pseudonymises personal data *inside* existing rows (the event stays; the
    email/name in it doesn't), and a retention policy an org admin configured
    purges rows older than the retention period."""
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    actor_name = Column(String, nullable=True)
    action = Column(String, nullable=False)  # e.g. "deal.stage_changed", "po.approved"
    resource_type = Column(String, nullable=False)
    resource_id = Column(Integer, nullable=True)
    before = Column(String, nullable=True)  # JSON string
    after = Column(String, nullable=True)   # JSON string
    created_at = Column(DateTime, default=datetime.utcnow)


class PurchaseOrder(Base):
    """Segregation of duties is enforced here, not just described: the
    approve endpoint rejects the request if approver == requester."""
    __tablename__ = "erp_purchase_orders"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    vendor_id = Column(Integer, ForeignKey("erp_vendors.id"), nullable=False)
    requested_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    approved_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    description = Column(String, nullable=False)
    total = Column(Float, nullable=False)
    status = Column(String, default="Pending")  # Pending, Approved, Rejected
    created_at = Column(DateTime, default=datetime.utcnow)

    vendor = relationship("Vendor")


class Document(Base):
    """Now backed by real file storage (see storage.py) — storage_key
    points to actual bytes, not just a metadata record describing a file
    that doesn't exist anywhere."""
    __tablename__ = "dms_documents"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    party_id = Column(Integer, ForeignKey("parties.id"), nullable=True)
    title = Column(String, nullable=False)
    category = Column(String, default="General")
    version = Column(Integer, default=1)
    uploaded_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    storage_key = Column(String, nullable=True)
    filename = Column(String, nullable=True)
    mime_type = Column(String, nullable=True)
    size_bytes = Column(Integer, nullable=True)

    party = relationship("Party")


class Project(Base):
    __tablename__ = "pm_projects"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    name = Column(String, nullable=False)
    status = Column(String, default="Active")  # Active, Completed, On Hold
    budget = Column(Float, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class Task(Base):
    __tablename__ = "pm_tasks"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    project_id = Column(Integer, ForeignKey("pm_projects.id"), nullable=False)
    title = Column(String, nullable=False)
    status = Column(String, default="To Do")  # To Do, In Progress, Done
    assigned_to_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    due_date = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Campaign(Base):
    __tablename__ = "mkt_campaigns"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    name = Column(String, nullable=False)
    channel = Column(String, default="Email")  # Email, Social, SMS
    status = Column(String, default="Draft")  # Draft, Active, Completed
    budget = Column(Float, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class Article(Base):
    __tablename__ = "kb_articles"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    title = Column(String, nullable=False)
    content = Column(String, nullable=False)
    visibility = Column(String, default="internal")  # internal, public
    author_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class ApiKey(Base):
    """Backs both API Manager (admin-issued keys) and Developer Portal
    (self-serve keys) — same table, different issuance flow, which is the
    honest way to avoid building two parallel key systems."""
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    name = Column(String, nullable=False)
    key_prefix = Column(String, nullable=False)  # shown in UI, e.g. "sk_slate_a1b2"
    hashed_key = Column(String, nullable=False)  # full key is never stored/shown again
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    is_self_serve = Column(String, default="false")  # "true" for Developer Portal-issued keys
    revoked = Column(String, default="false")
    usage_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_used_at = Column(DateTime, nullable=True)


class AutomationRule(Base):
    """Deliberately scoped down from a visual workflow canvas to one real,
    wired trigger family: reacting to a CRM deal's stage changing. The
    pattern (rule stored in DB, checked on a real event, action executed)
    is the same one a fuller engine would use — just one trigger type
    instead of an arbitrary graph of them."""
    __tablename__ = "automation_rules"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    name = Column(String, nullable=False)
    trigger_stage = Column(String, nullable=False)  # e.g. "Won"
    action_type = Column(String, default="create_task")  # create_task, log_only
    action_note = Column(String, nullable=True)
    enabled = Column(String, default="true")
    run_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class Agent(Base):
    """A configured persona invoked through the same real Copilot
    infrastructure everything else uses — not a separate orchestration
    engine. Multi-agent hand-off is the natural next step on top of this,
    not a rebuild of it."""
    __tablename__ = "ai_agents"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    name = Column(String, nullable=False)
    persona_prompt = Column(String, nullable=False)
    enabled = Column(String, default="true")
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class FeatureFlag(Base):
    """Per-tenant override for a flag key defined in feature_flags.py's
    registry. No row means 'use the registry default' — toggling a flag
    for the first time is what creates the row, so every existing tenant
    and every tenant that signs up after a new flag is added starts on
    the documented default with no seeding or migration step required."""
    __tablename__ = "feature_flags"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    key = Column(String, nullable=False)
    enabled = Column(Boolean, nullable=False)
    updated_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (UniqueConstraint("tenant_id", "key", name="uq_feature_flag_tenant_key"),)


class TenantBranding(Base):
    """One row per tenant, created lazily on first save (see
    routers/branding.py). Real white-labeling — name, logo, and two
    colors that flow into the actual rendered UI via CSS custom
    properties — not a source-code find-and-replace of 'Slate'."""
    __tablename__ = "tenant_branding"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, unique=True)
    display_name = Column(String, nullable=True)
    logo_storage_key = Column(String, nullable=True)
    logo_mime_type = Column(String, nullable=True)
    primary_color = Column(String, nullable=True)
    secondary_color = Column(String, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class InstalledPlugin(Base):
    """Per-tenant enable state for a plugin discovered on disk by
    plugins_loader.py. Absence of a row means 'enabled' — same
    lazy-default pattern as FeatureFlag, so a freshly dropped-in plugin
    is live for every tenant immediately, and disabling it for one
    tenant doesn't touch any other tenant's row."""
    __tablename__ = "installed_plugins"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    plugin_key = Column(String, nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (UniqueConstraint("tenant_id", "plugin_key", name="uq_installed_plugin_tenant_key"),)


class AgentRun(Base):
    """A planner -> executor hand-off: one planning call breaks a goal
    into ordered steps, then each step is executed in turn by an
    existing Agent Builder persona (or a built-in default executor if
    the tenant has none configured / none fit), with each step's output
    handed forward as context to the next — real sequential hand-off on
    top of the same Claude call every other AI feature here uses, not a
    separate orchestration engine."""
    __tablename__ = "agent_runs"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    goal = Column(String, nullable=False)
    status = Column(String, default="planning")  # planning, running, completed, failed
    plan_summary = Column(String, nullable=True)
    final_result = Column(String, nullable=True)
    error = Column(String, nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)


class AgentRunStep(Base):
    __tablename__ = "agent_run_steps"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    run_id = Column(Integer, ForeignKey("agent_runs.id"), nullable=False)
    step_number = Column(Integer, nullable=False)
    title = Column(String, nullable=False)
    instructions = Column(String, nullable=False)
    assigned_agent_id = Column(Integer, ForeignKey("ai_agents.id"), nullable=True)
    assigned_agent_name = Column(String, nullable=False)
    status = Column(String, default="pending")  # pending, running, done, failed
    output = Column(String, nullable=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)


class PartyProfile(Base):
    """Marketing-specific enrichment on top of the shared Party record —
    a new table, not new columns on `parties`, so this doesn't need a
    migration for any party that already exists. One row per party,
    created lazily the first time someone sets a value on it."""
    __tablename__ = "party_profiles"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    party_id = Column(Integer, ForeignKey("parties.id"), nullable=False, unique=True)
    industry = Column(String, nullable=True)
    company_size = Column(String, nullable=True)  # "1-10" | "11-50" | "51-200" | "201-1000" | "1000+"
    tags = Column(String, nullable=True)  # stored as ",tag-one,tag-two," — see marketing.py _normalize_tags
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Segment(Base):
    """A saved query, not a static list: matching parties are computed
    live at read time (see routers/marketing.py's _segment_query), so a
    segment's count and membership always reflect current data instead
    of drifting from a snapshot taken when it was created."""
    __tablename__ = "segments"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    name = Column(String, nullable=False)
    filter_industry = Column(String, nullable=True)
    filter_company_size = Column(String, nullable=True)
    filter_tag = Column(String, nullable=True)
    require_email = Column(Boolean, nullable=False, default=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class LdapConfig(Base):
    """One row per tenant, created lazily on first save — same pattern as
    TenantBranding. bind_password_encrypted uses the exact same
    field-level encryption (security.py's encrypt_field/decrypt_field)
    already used for vendor bank details: a second real usage of that
    pattern, not a new one invented for this table."""
    __tablename__ = "ldap_configs"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, unique=True)
    server_uri = Column(String, nullable=False)  # e.g. "ldap://ldap.acme.com:389"
    bind_dn = Column(String, nullable=False)  # service account used to search for the logging-in user
    bind_password_encrypted = Column(String, nullable=True)
    user_search_base = Column(String, nullable=False)  # e.g. "ou=people,dc=acme,dc=com"
    user_search_filter = Column(String, nullable=False, default="(uid={username})")
    email_attribute = Column(String, nullable=False, default="mail")
    name_attribute = Column(String, nullable=False, default="cn")
    enabled = Column(Boolean, nullable=False, default=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SamlConfig(Base):
    """One row per tenant, same lazy-created pattern as LdapConfig. Only
    the IdP's public cert is stored (used to verify signatures on
    incoming assertions) — no private key or secret ever touches this
    table, since the SP side of SAML never holds the IdP's key."""
    __tablename__ = "saml_configs"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, unique=True)
    idp_entity_id = Column(String, nullable=False)
    idp_sso_url = Column(String, nullable=False)
    idp_x509_cert = Column(String, nullable=False)  # PEM, used to verify the IdP's signature
    email_attribute = Column(String, nullable=False, default="email")
    name_attribute = Column(String, nullable=False, default="displayName")
    enabled = Column(Boolean, nullable=False, default=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AIProviderConfig(Base):
    """One row per tenant, lazily created — same pattern as every other
    tenant-scoped setting table here. A new table rather than new columns
    on Tenant (which already has a `preferred_model` column from before
    multi-provider support existed) for the usual reason: create_all()
    doesn't alter existing tables, so extending Tenant directly would
    silently do nothing for any tenant whose row already exists.
    Tenant.preferred_model itself is left in place, untouched, but no
    longer read or written by the multi-provider code path — see
    routers/ai_studio.py for the fallback that still honors it once,
    the first time this table has no row yet."""
    __tablename__ = "ai_provider_configs"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, unique=True)
    provider = Column(String, nullable=False, default="anthropic")  # "anthropic" | "openai" | "gemini"
    model = Column(String, nullable=True)  # falls back to that provider's own default when unset
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class WebAuthnCredential(Base):
    """A registered passkey. One user can register several (a laptop's
    platform authenticator, a phone, a hardware key) — unlike the other
    auth-config tables, this is one row per *credential*, not one row
    per tenant. credential_data stores fido2's AttestedCredentialData
    raw (it's a bytes subclass — see backend/webauthn_auth.py — so this
    round-trips through storage with no extra encoding), which is what
    the fido2 server library needs handed back for every subsequent
    authentication attempt in order to verify a signature against the
    right public key."""
    __tablename__ = "webauthn_credentials"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    credential_data = Column(LargeBinary, nullable=False)
    sign_count = Column(Integer, nullable=False, default=0)
    nickname = Column(String, nullable=True)  # "MacBook Touch ID", set by the user at registration time
    created_at = Column(DateTime, default=datetime.utcnow)
    last_used_at = Column(DateTime, nullable=True)


class OAuthConfig(Base):
    """One row per tenant, created lazily on first save — same pattern as
    LdapConfig/SamlConfig. Slate is the OAuth client (relying party); the
    tenant's own OpenID Connect provider (Google, Microsoft, Okta, Auth0,
    Keycloak, ...) is configured here with explicit endpoints, no
    discovery-document fetch. client_secret_encrypted uses the same
    field-level encryption (security.py's encrypt_field/decrypt_field)
    as LdapConfig.bind_password_encrypted and vendor bank details —
    unlike SAML (public cert only), OAuth *does* leave a real secret on
    the client side, so it gets the same treatment LDAP's service
    account password does. The secret is write-only through the API:
    reads only ever expose a has_client_secret boolean."""
    __tablename__ = "oauth_configs"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, unique=True)
    provider_name = Column(String, nullable=False)  # display label only, e.g. "Google Workspace"
    client_id = Column(String, nullable=False)
    client_secret_encrypted = Column(String, nullable=True)
    authorization_endpoint = Column(String, nullable=False)
    token_endpoint = Column(String, nullable=False)
    jwks_uri = Column(String, nullable=False)  # fetched live on every sign-in, never cached — see oauth_auth.py
    issuer = Column(String, nullable=False)  # must equal the ID token's `iss` exactly
    scopes = Column(String, nullable=False, default="openid email profile")
    enabled = Column(Boolean, nullable=False, default=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CustomModelEndpoint(Base):
    """A tenant's own OpenAI-compatible model server — a self-hosted vLLM,
    Ollama, LM Studio, llama.cpp, LiteLLM gateway, an Azure/OpenRouter-style
    proxy, or a fine-tuned model behind any of them. Covers 'local LLMs and
    custom models' without a code change per backend: they all speak the
    same /chat/completions (and /embeddings) wire format. kind='chat'
    endpoints can be selected as the workspace's provider in AI Studio;
    kind='embedding' endpoints feed the knowledge-base vector index.
    The key (if the server wants one) gets the same field-level encryption
    as every other stored secret and is write-only through the API."""
    __tablename__ = "custom_model_endpoints"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_custom_endpoint_name"),)

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    name = Column(String, nullable=False)
    kind = Column(String, nullable=False, default="chat")  # chat | embedding
    base_url = Column(String, nullable=False)
    model = Column(String, nullable=False)
    api_key_encrypted = Column(String, nullable=True)
    auth_header = Column(String, nullable=False, default="Authorization")  # "Authorization" -> Bearer <key>; any other name carries the raw key (e.g. api-key)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Workflow(Base):
    """Automation Studio's workflow engine (workflow_engine.py): a directed acyclic graph of
    nodes, n8n/Zapier-style. definition is the whole graph as JSON text (kept as Text so it
    behaves identically on SQLite and Postgres); trigger_type/trigger_config are extracted from
    the trigger node on every save so the scheduler and event dispatcher can query them without
    parsing every definition. New workflows start disabled."""
    __tablename__ = "workflows"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_workflow_tenant_name"),)

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    definition = Column(String, nullable=False)
    trigger_type = Column(String, nullable=False, default="manual")  # manual | webhook | schedule | event
    trigger_config = Column(String, nullable=False, default="{}")
    enabled = Column(Boolean, nullable=False, default=False)
    version = Column(Integer, nullable=False, default=1)
    webhook_id = Column(String, unique=True, index=True, nullable=False)  # the public, unguessable URL segment
    webhook_secret_encrypted = Column(String, nullable=False)  # HMAC key for signed webhook deliveries
    next_run_at = Column(DateTime, nullable=True, index=True)
    last_run_at = Column(DateTime, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class WorkflowRun(Base):
    """One execution. definition is a snapshot of the graph as it was when the run started, so
    editing a workflow never rewrites history. heartbeat_at is touched every few seconds while a
    run executes: a 'running' row whose heartbeat has gone stale belongs to a worker that died,
    and is reaped — safe with several replicas, unlike 'fail every running row at startup'."""
    __tablename__ = "workflow_runs"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    workflow_id = Column(Integer, ForeignKey("workflows.id"), nullable=False, index=True)
    workflow_version = Column(Integer, nullable=False, default=1)
    status = Column(String, nullable=False, default="queued")  # queued | running | succeeded | failed
    trigger_type = Column(String, nullable=False, default="manual")
    trigger_data = Column(String, nullable=True)
    definition = Column(String, nullable=False)
    output = Column(String, nullable=True)
    error = Column(String, nullable=True)
    heartbeat_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)


class WorkflowNodeRun(Base):
    """Per-node record: what ran, with what (rendered, redacted) input, what came out, how many
    attempts it took. This is what makes a failed run debuggable instead of mysterious."""
    __tablename__ = "workflow_node_runs"

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("workflow_runs.id"), nullable=False, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    node_id = Column(String, nullable=False)
    node_type = Column(String, nullable=False)
    status = Column(String, nullable=False, default="running")  # running | succeeded | failed | skipped
    attempts = Column(Integer, nullable=False, default=0)
    input = Column(String, nullable=True)
    output = Column(String, nullable=True)
    error = Column(String, nullable=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    duration_ms = Column(Integer, nullable=True)


class WorkflowCredential(Base):
    """A secret a workflow's HTTP node can use without the secret ever appearing in the workflow
    definition, a run record, or an API response. Each credential is bound to the hosts it may
    be sent to — without that, anyone allowed to build workflows could aim 'use credential X' at
    their own server and read the secret off the request."""
    __tablename__ = "workflow_credentials"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_workflow_credential_name"),)

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    name = Column(String, nullable=False)
    kind = Column(String, nullable=False)  # bearer | header | basic
    header_name = Column(String, nullable=True)  # for kind=header
    username = Column(String, nullable=True)  # for kind=basic
    secret_encrypted = Column(String, nullable=False)
    allowed_hosts = Column(String, nullable=False, default="[]")  # JSON list; "api.example.com" or "*.example.com"
    created_at = Column(DateTime, default=datetime.utcnow)


class RagDocument(Base):
    """One thing in the knowledge index: an uploaded file, pasted text, or a KB article. source_key is
    the identity used to replace it in place when it changes ('kb:<article id>' or 'title:<title>').
    embedding_model records which embedder produced its vectors — vectors from different models live in
    different spaces and must never be compared, so search only considers documents whose model matches
    the workspace's current embedder (the rest are reported as stale, and reindexed on request).
    min_role is the lowest role allowed to *retrieve* it: access control is applied at retrieval, not
    after the fact."""
    __tablename__ = "rag_documents"
    __table_args__ = (UniqueConstraint("tenant_id", "source_key", name="uq_rag_document_source"),)

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    source_type = Column(String, nullable=False)  # upload | text | kb_article
    source_key = Column(String, nullable=False)
    source_id = Column(Integer, nullable=True)
    title = Column(String, nullable=False)
    filename = Column(String, nullable=True)
    content_hash = Column(String, nullable=False)
    min_role = Column(String, nullable=False, default="standard_user")
    status = Column(String, nullable=False, default="indexed")
    chunk_count = Column(Integer, nullable=False, default=0)
    char_count = Column(Integer, nullable=False, default=0)
    embedding_model = Column(String, nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class RagChunk(Base):
    """A retrievable passage. embedding is float32 bytes when the vector store is the database; NULL when
    the vectors live in an external vector database (the text and metadata always stay here)."""
    __tablename__ = "rag_chunks"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    document_id = Column(Integer, ForeignKey("rag_documents.id"), nullable=False, index=True)
    ordinal = Column(Integer, nullable=False)
    page = Column(Integer, nullable=True)
    text = Column(String, nullable=False)
    embedding = Column(LargeBinary, nullable=True)
    dim = Column(Integer, nullable=True)


class UserLock(Base):
    """Instant access revocation — the Zero Trust building block. A locked user's tokens stop working on the
    very next request (auth.get_current_user, the WebSocket, and password login all check this table), without
    waiting for a JWT to expire. Erasure creates one too. One row per user."""
    __tablename__ = "user_locks"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True)
    reason = Column(String, nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class DataSubjectRequest(Base):
    """A GDPR export/erasure request. Stores a hash and a masked form of the subject's email — the record of an
    erasure must not itself retain the personal data that was erased."""
    __tablename__ = "compliance_dsars"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    kind = Column(String, nullable=False)  # export | erasure
    subject_hash = Column(String, nullable=False)
    subject_masked = Column(String, nullable=False)
    status = Column(String, nullable=False, default="completed")  # completed | refused
    requested_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    summary = Column(String, nullable=True)  # JSON
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)


class RetentionPolicy(Base):
    __tablename__ = "retention_policies"
    __table_args__ = (UniqueConstraint("tenant_id", "entity", name="uq_retention_tenant_entity"),)

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    entity = Column(String, nullable=False)  # audit_log | workflow_runs
    days = Column(Integer, nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    last_run_at = Column(DateTime, nullable=True)
    last_deleted = Column(Integer, nullable=True)


class ConsentRecord(Base):
    """Append-only consent ledger: a withdrawal is a new row, never an edit, so the history of what a person
    agreed to, when, and where it was recorded is itself evidence."""
    __tablename__ = "consent_records"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    subject_email = Column(String, nullable=False, index=True)
    purpose = Column(String, nullable=False)
    granted = Column(Boolean, nullable=False)
    source = Column(String, nullable=True)
    recorded_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class LegalHold(Base):
    """While a hold is active, erasure of that subject is refused — retention obligations outrank a request."""
    __tablename__ = "legal_holds"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    subject_email = Column(String, nullable=False, index=True)
    reason = Column(String, nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    released_at = Column(DateTime, nullable=True)


class AccessReview(Base):
    """A periodic user-access review (SOC 2 CC6.2 / ISO 27001 A.5.18): a snapshot of who has what access, and a
    keep / change-role / revoke decision on each, by someone other than the person being reviewed."""
    __tablename__ = "access_reviews"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    title = Column(String, nullable=False)
    status = Column(String, nullable=False, default="open")  # open | completed
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)


class AccessReviewItem(Base):
    __tablename__ = "access_review_items"

    id = Column(Integer, primary_key=True)
    review_id = Column(Integer, ForeignKey("access_reviews.id"), nullable=False, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    email = Column(String, nullable=False)
    name = Column(String, nullable=False)
    role_at_review = Column(String, nullable=False)
    mfa_enabled = Column(Boolean, nullable=False, default=False)
    decision = Column(String, nullable=True)  # keep | change_role | revoke
    new_role = Column(String, nullable=True)
    decided_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    decided_at = Column(DateTime, nullable=True)


class ControlAttestation(Base):
    """A human statement that a control we can't measure from inside the app (backups, vendor review, change
    management) is operating. Valid for 365 days, then the control reverts to 'manual'."""
    __tablename__ = "control_attestations"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    control_id = Column(String, nullable=False)
    note = Column(String, nullable=False)
    attested_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class BreachIncident(Base):
    """GDPR Art. 33: the supervisory authority must be told within 72 hours of becoming aware of a personal-data
    breach. The register computes the deadline from discovered_at and the compliance controls fail when it passes."""
    __tablename__ = "breach_incidents"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    title = Column(String, nullable=False)
    description = Column(String, nullable=True)
    severity = Column(String, nullable=False, default="medium")  # low | medium | high
    affected_count = Column(Integer, nullable=True)
    status = Column(String, nullable=False, default="open")  # open | closed
    discovered_at = Column(DateTime, nullable=False)
    authority_notified_at = Column(DateTime, nullable=True)
    subjects_notified_at = Column(DateTime, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class MLDataset(Base):
    """A tabular dataset a workspace trains on: CSV text, bounded (50k rows, 100 columns, 5 MB). content_hash is
    recorded on every model version trained from it, so 'which exact data produced this model' is answerable."""
    __tablename__ = "ml_datasets"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_ml_dataset_name"),)

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    name = Column(String, nullable=False)
    content = Column(String, nullable=False)
    content_hash = Column(String, nullable=False)
    n_rows = Column(Integer, nullable=False)
    columns = Column(String, nullable=False)  # JSON: [{name, kind, missing, unique}]
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class MLModel(Base):
    __tablename__ = "ml_models"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_ml_model_name"),)

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    task = Column(String, nullable=False)  # classification | regression
    target = Column(String, nullable=False)
    min_score = Column(Float, nullable=True)  # quality gate: promotion to production needs primary metric >= this
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class MLModelVersion(Base):
    """One training run = one immutable version: its algorithm and parameters, the dataset it saw (id + hash), the
    feature schema, holdout metrics, the baseline distributions drift is measured against, and the signed artifact.
    stage is the only thing that changes afterwards (none -> staging -> production -> archived)."""
    __tablename__ = "ml_model_versions"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    model_id = Column(Integer, ForeignKey("ml_models.id"), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    dataset_id = Column(Integer, ForeignKey("ml_datasets.id"), nullable=True)
    dataset_hash = Column(String, nullable=True)
    status = Column(String, nullable=False, default="training")  # training | ready | failed
    error = Column(String, nullable=True)
    algorithm = Column(String, nullable=True)
    params = Column(String, nullable=True)
    schema = Column(String, nullable=True)
    metrics = Column(String, nullable=True)
    baseline = Column(String, nullable=True)
    artifact = Column(LargeBinary, nullable=True)
    artifact_sig = Column(String, nullable=True)
    stage = Column(String, nullable=False, default="none")
    train_rows = Column(Integer, nullable=True)
    duration_ms = Column(Integer, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class MLPrediction(Base):
    """Every served prediction is logged (inputs as the model saw them, output, version) — the raw material of
    drift and performance monitoring. actual is filled in later when the ground truth becomes known."""
    __tablename__ = "ml_predictions"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    model_id = Column(Integer, ForeignKey("ml_models.id"), nullable=False, index=True)
    version_id = Column(Integer, ForeignKey("ml_model_versions.id"), nullable=False, index=True)
    inputs = Column(String, nullable=False)
    output = Column(String, nullable=False)
    actual = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
