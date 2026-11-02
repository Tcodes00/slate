from datetime import datetime
from typing import Optional
from pydantic import BaseModel, EmailStr


class SignupRequest(BaseModel):
    tenant_name: str
    name: str
    email: EmailStr
    password: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: int
    name: str
    email: str
    role: str
    tenant: Optional[str] = None
    email_verified: bool = True


class InviteRequest(BaseModel):
    name: str
    email: EmailStr
    password: str
    role: str = "standard_user"


class TeammateOut(BaseModel):
    id: int
    name: str
    email: str
    role: str


class RequestPasswordReset(BaseModel):
    email: EmailStr


class ConfirmPasswordReset(BaseModel):
    token: str
    new_password: str


class VerifyEmail(BaseModel):
    token: str


class InventoryItemCreate(BaseModel):
    sku: str
    name: str
    unit_of_measure: str = "unit"
    quantity_on_hand: float = 0
    reorder_point: float = 0
    unit_cost: float = 0


class InventoryItemOut(BaseModel):
    id: int
    sku: str
    name: str
    unit_of_measure: str
    quantity_on_hand: float
    reorder_point: float
    unit_cost: float
    low_stock: bool


class MovementCreate(BaseModel):
    movement_type: str  # in, out, adjustment
    quantity: float
    note: Optional[str] = None


class MovementOut(BaseModel):
    id: int
    movement_type: str
    quantity: float
    note: Optional[str] = None
    occurred_at: datetime


class QuoteCreate(BaseModel):
    deal_id: Optional[int] = None
    company: Optional[str] = None
    party_id: Optional[int] = None
    description: str
    amount: float


class QuoteUpdate(BaseModel):
    status: str  # Draft, Sent, Accepted, Declined


class QuoteOut(BaseModel):
    id: int
    quote_number: str
    company: str
    description: str
    amount: float
    status: str
    created_at: datetime


class OrderOut(BaseModel):
    id: int
    order_number: str
    company: str
    description: str
    amount: float
    status: str
    created_at: datetime


class OrderUpdate(BaseModel):
    status: str  # Pending, Fulfilled, Cancelled


class EmployeeCreate(BaseModel):
    name: str
    email: Optional[str] = None
    employee_number: str
    department: Optional[str] = None
    job_title: Optional[str] = None
    manager_id: Optional[int] = None
    hire_date: Optional[datetime] = None


class EmployeeUpdate(BaseModel):
    department: Optional[str] = None
    job_title: Optional[str] = None
    manager_id: Optional[int] = None
    status: Optional[str] = None


class EmployeeOut(BaseModel):
    id: int
    name: str
    email: Optional[str] = None
    employee_number: str
    department: Optional[str] = None
    job_title: Optional[str] = None
    manager_id: Optional[int] = None
    manager_name: Optional[str] = None
    status: str


class LeaveRequestCreate(BaseModel):
    employee_id: int
    leave_type: str = "PTO"
    start_date: datetime
    end_date: datetime
    reason: Optional[str] = None


class LeaveRequestOut(BaseModel):
    id: int
    employee_id: int
    employee_name: str
    leave_type: str
    start_date: datetime
    end_date: datetime
    status: str
    reason: Optional[str] = None


class LeaveRequestUpdate(BaseModel):
    status: str  # Approved, Denied


class TicketCreate(BaseModel):
    subject: str
    company: Optional[str] = None
    party_id: Optional[int] = None
    priority: str = "Normal"


class TicketUpdate(BaseModel):
    status: Optional[str] = None
    priority: Optional[str] = None


class TicketOut(BaseModel):
    id: int
    subject: str
    company: Optional[str] = None
    status: str
    priority: str
    created_at: datetime
    updated_at: datetime
    message_count: int


class TicketMessageCreate(BaseModel):
    sender: str
    content: str


class TicketMessageOut(BaseModel):
    id: int
    sender: str
    content: str
    created_at: datetime


class VendorCreate(BaseModel):
    company: str
    payment_terms: str = "Net 30"
    bank_details: Optional[str] = None


class VendorOut(BaseModel):
    id: int
    company: str
    payment_terms: str
    rating: float
    has_bank_details: bool
    bank_details: Optional[str] = None


class POCreate(BaseModel):
    vendor_id: int
    description: str
    total: float


class POOut(BaseModel):
    id: int
    vendor: str
    description: str
    total: float
    status: str
    requested_by: str
    approved_by: Optional[str] = None
    created_at: datetime


class DocumentOut(BaseModel):
    id: int
    title: str
    category: str
    version: int
    company: Optional[str] = None
    uploaded_by: str
    filename: Optional[str] = None
    size_bytes: Optional[int] = None
    has_file: bool
    created_at: datetime


class ProjectCreate(BaseModel):
    name: str
    budget: float = 0


class ProjectOut(BaseModel):
    id: int
    name: str
    status: str
    budget: float
    task_count: int
    done_count: int


class TaskCreate(BaseModel):
    title: str
    due_date: Optional[datetime] = None


class TaskUpdate(BaseModel):
    status: str  # To Do, In Progress, Done


class TaskOut(BaseModel):
    id: int
    project_id: int
    title: str
    status: str
    due_date: Optional[datetime] = None


class CampaignCreate(BaseModel):
    name: str
    channel: str = "Email"
    budget: float = 0


class CampaignUpdate(BaseModel):
    status: str  # Draft, Active, Completed


class CampaignOut(BaseModel):
    id: int
    name: str
    channel: str
    status: str
    budget: float
    created_at: datetime


class ArticleCreate(BaseModel):
    title: str
    content: str
    visibility: str = "internal"


class ArticleOut(BaseModel):
    id: int
    title: str
    content: str
    visibility: str
    author: str
    created_at: datetime


class ApiKeyCreate(BaseModel):
    name: str


class ApiKeyCreatedOut(BaseModel):
    id: int
    name: str
    key_prefix: str
    full_key: str  # shown exactly once, at creation


class ApiKeyOut(BaseModel):
    id: int
    name: str
    key_prefix: str
    is_self_serve: bool
    revoked: bool
    usage_count: int
    created_at: datetime
    last_used_at: Optional[datetime] = None


class AIProviderOut(BaseModel):
    key: str
    name: str
    default_model: str
    is_configured: bool


class AIStudioSettings(BaseModel):
    provider: str
    model: str
    available_providers: list[AIProviderOut]


class AIStudioUpdate(BaseModel):
    provider: str
    model: Optional[str] = None  # omit to use that provider's own default


class RuleCreate(BaseModel):
    name: str
    trigger_stage: str
    action_type: str = "create_task"
    action_note: Optional[str] = None


class RuleUpdate(BaseModel):
    enabled: bool


class RuleOut(BaseModel):
    id: int
    name: str
    trigger_stage: str
    action_type: str
    action_note: Optional[str] = None
    enabled: bool
    run_count: int


class AgentCreate(BaseModel):
    name: str
    persona_prompt: str


class AgentUpdate(BaseModel):
    enabled: bool


class AgentOut(BaseModel):
    id: int
    name: str
    persona_prompt: str
    enabled: bool
    created_at: datetime


class AgentInvoke(BaseModel):
    message: str


class AgentInvokeOut(BaseModel):
    reply: str


class TokenResponse(BaseModel):
    access_token: Optional[str] = None
    token_type: str = "bearer"
    user: Optional[UserOut] = None
    mfa_required: bool = False
    mfa_token: Optional[str] = None


class MFASetupOut(BaseModel):
    secret: str
    provisioning_uri: str


class MFAConfirm(BaseModel):
    code: str


class MFALoginVerify(BaseModel):
    mfa_token: str
    code: str


class DealCreate(BaseModel):
    name: str
    company: str
    value: float = 0
    stage: str = "Lead"


class DealUpdate(BaseModel):
    name: Optional[str] = None
    stage: Optional[str] = None
    value: Optional[float] = None


class DealOut(BaseModel):
    id: int
    name: str
    company: str
    contact_email: Optional[str] = None
    value: float
    stage: str
    created_at: datetime
    last_activity_at: datetime


class ActivityCreate(BaseModel):
    content: str


class ActivityOut(BaseModel):
    id: int
    content: str
    occurred_at: datetime


class InvoiceCreate(BaseModel):
    company: Optional[str] = None
    party_id: Optional[int] = None
    deal_id: Optional[int] = None
    description: str
    amount: float
    due_date: Optional[datetime] = None


class InvoiceUpdate(BaseModel):
    status: Optional[str] = None
    amount: Optional[float] = None
    due_date: Optional[datetime] = None


class InvoiceOut(BaseModel):
    id: int
    invoice_number: str
    company: str
    description: str
    amount: float
    status: str
    due_date: Optional[datetime] = None
    issued_at: datetime
    paid_at: Optional[datetime] = None
    is_overdue: bool


class FeatureFlagOut(BaseModel):
    key: str
    label: str
    description: str
    enabled: bool


class FeatureFlagUpdate(BaseModel):
    enabled: bool


class BrandingOut(BaseModel):
    display_name: str
    has_logo: bool
    primary_color: str
    secondary_color: str
    is_customized: bool


class BrandingUpdate(BaseModel):
    display_name: Optional[str] = None
    primary_color: Optional[str] = None
    secondary_color: Optional[str] = None


class PluginOut(BaseModel):
    key: str
    name: str
    version: str
    description: str
    route_count: int
    load_error: Optional[str] = None
    enabled: bool


class PluginUpdate(BaseModel):
    enabled: bool


class AgentRunCreate(BaseModel):
    goal: str


class AgentRunStepOut(BaseModel):
    id: int
    step_number: int
    title: str
    assigned_agent_name: str
    status: str
    output: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class AgentRunOut(BaseModel):
    id: int
    goal: str
    status: str
    plan_summary: Optional[str] = None
    final_result: Optional[str] = None
    error: Optional[str] = None
    created_at: datetime
    completed_at: Optional[datetime] = None
    steps: list[AgentRunStepOut] = []


class PartyWithProfileOut(BaseModel):
    id: int
    display_name: str
    primary_email: Optional[str] = None
    industry: Optional[str] = None
    company_size: Optional[str] = None
    tags: list[str] = []


class PartyProfileUpdate(BaseModel):
    industry: Optional[str] = None
    company_size: Optional[str] = None
    tags: Optional[list[str]] = None


class SegmentCreate(BaseModel):
    name: str
    filter_industry: Optional[str] = None
    filter_company_size: Optional[str] = None
    filter_tag: Optional[str] = None
    require_email: bool = True


class SegmentOut(BaseModel):
    id: int
    name: str
    filter_industry: Optional[str] = None
    filter_company_size: Optional[str] = None
    filter_tag: Optional[str] = None
    require_email: bool
    member_count: int
    created_at: datetime


class LdapConfigOut(BaseModel):
    server_uri: str
    bind_dn: str
    has_bind_password: bool
    user_search_base: str
    user_search_filter: str
    email_attribute: str
    name_attribute: str
    enabled: bool
    is_configured: bool


class LdapConfigUpdate(BaseModel):
    server_uri: str
    bind_dn: str
    bind_password: Optional[str] = None  # omit to keep the currently-saved password unchanged
    user_search_base: str
    user_search_filter: str = "(uid={username})"
    email_attribute: str = "mail"
    name_attribute: str = "cn"
    enabled: bool = False


class LdapTestResult(BaseModel):
    success: bool
    message: str


class LdapLoginRequest(BaseModel):
    tenant_slug: str
    username: str
    password: str


class SamlConfigOut(BaseModel):
    idp_entity_id: str
    idp_sso_url: str
    has_cert: bool
    email_attribute: str
    name_attribute: str
    enabled: bool
    is_configured: bool
    sp_entity_id: Optional[str] = None
    acs_url: Optional[str] = None


class SamlConfigUpdate(BaseModel):
    idp_entity_id: str
    idp_sso_url: str
    idp_x509_cert: str
    email_attribute: str = "email"
    name_attribute: str = "displayName"
    enabled: bool = False


class OAuthConfigOut(BaseModel):
    provider_name: str
    client_id: str
    has_client_secret: bool
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    issuer: str
    scopes: str
    enabled: bool
    is_configured: bool
    redirect_uri: Optional[str] = None  # what the org admin registers at their provider


class OAuthConfigUpdate(BaseModel):
    provider_name: str
    client_id: str
    client_secret: Optional[str] = None  # omit to keep the currently-saved secret unchanged
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    issuer: str
    scopes: str = "openid email profile"
    enabled: bool = False


class WebAuthnRegisterCompleteRequest(BaseModel):
    challenge_id: str
    credential: dict
    nickname: Optional[str] = None


class WebAuthnLoginBeginRequest(BaseModel):
    email: str


class WebAuthnLoginCompleteRequest(BaseModel):
    challenge_id: str
    email: str
    credential: dict


class WebAuthnCredentialOut(BaseModel):
    id: int
    nickname: Optional[str] = None
    created_at: datetime
    last_used_at: Optional[datetime] = None


class ModelEndpointCreate(BaseModel):
    name: str
    kind: str = "chat"
    base_url: str
    model: str
    api_key: Optional[str] = None
    auth_header: str = "Authorization"


class ModelEndpointUpdate(BaseModel):
    name: Optional[str] = None
    model: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None  # omit to keep the saved key
    enabled: Optional[bool] = None


class ModelEndpointOut(BaseModel):
    id: int
    name: str
    kind: str
    base_url: str
    model: str
    has_api_key: bool
    auth_header: str
    enabled: bool


class WorkflowCreate(BaseModel):
    name: str
    description: str = ""
    definition: dict


class WorkflowUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    definition: Optional[dict] = None


class WorkflowEnabled(BaseModel):
    enabled: bool


class WorkflowRunRequest(BaseModel):
    input: dict = {}
    wait: bool = False


class WorkflowValidateRequest(BaseModel):
    definition: dict


class WorkflowCredentialCreate(BaseModel):
    name: str
    kind: str
    secret: str
    allowed_hosts: list[str]
    header_name: Optional[str] = None
    username: Optional[str] = None


class RagTextIn(BaseModel):
    title: str
    text: str
    min_role: str = "standard_user"


class RagSearchIn(BaseModel):
    query: str
    top_k: int = 5
    mode: str = "hybrid"  # hybrid | dense | lexical


class RagAskIn(BaseModel):
    question: str
    top_k: int = 5


class DsarRequest(BaseModel):
    email: str
    confirm: bool = False  # erasure only: must be explicitly true


class LegalHoldIn(BaseModel):
    email: str
    reason: str


class RetentionIn(BaseModel):
    entity: str
    days: int
    enabled: bool = True


class ConsentIn(BaseModel):
    email: str
    purpose: str
    granted: bool
    source: Optional[str] = None


class AccessReviewIn(BaseModel):
    title: str


class ReviewDecisionIn(BaseModel):
    decision: str
    new_role: Optional[str] = None


class AttestIn(BaseModel):
    note: str


class BreachIn(BaseModel):
    title: str
    description: str = ""
    severity: str = "medium"
    affected_count: Optional[int] = None
    discovered_at: Optional[str] = None  # ISO 8601; defaults to now


class BreachUpdate(BaseModel):
    authority_notified: Optional[bool] = None
    subjects_notified: Optional[bool] = None
    status: Optional[str] = None


class SuspendIn(BaseModel):
    reason: str
