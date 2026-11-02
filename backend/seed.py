"""Run once to create a demo tenant with a realistic pipeline: python seed.py"""

from datetime import datetime, timedelta

from auth import hash_password
from database import Base, SessionLocal, engine
import models

Base.metadata.create_all(bind=engine)
db = SessionLocal()
now = datetime.utcnow()


def days_ago(n):
    return now - timedelta(days=n)


# (company, deal name, value, email, [(stage, days_ago_of_transition), ...])
# The last tuple in the list is the deal's current stage.
DEALS = [
    ("Northgate Logistics", "Fleet Analytics Rollout", 18500, "daniel.osei@northgatelogistics.com",
     [("Lead", 22), ("Qualified", 15)]),
    ("Meridian Health", "Compliance Dashboard", 88000, "s.kim@meridianhealth.org",
     [("Lead", 12), ("Qualified", 9), ("Proposal", 6)]),
    ("Bluewave Retail", "POS Integration", 65000, "priya.nair@bluewaveretail.com",
     [("Lead", 35), ("Qualified", 28), ("Proposal", 18), ("Won", 9)]),
    ("Ferrous & Co", "Inventory Automation", 9000, "tom@ferrousco.com",
     [("Lead", 2)]),
    ("Coral Bay Hospitality", "Booking Sync", 27000, "j.okoro@coralbayhg.com",
     [("Lead", 8), ("Qualified", 6)]),
    ("Vantage Freight", "Route Optimization Pilot", 34000, "m.petrov@vantagefreight.com",
     [("Lead", 18), ("Qualified", 14), ("Proposal", 6)]),
    ("Solene Cosmetics", "Customer 360 Setup", 12500, "zara@solenecosmetics.com",
     [("Lead", 0)]),
    ("Delta Grain", "Supply Chain Visibility", 15000, "anika.rahman@deltagrain.com",
     [("Lead", 25), ("Qualified", 20), ("Lost", 14)]),
]

if db.query(models.Tenant).filter(models.Tenant.slug == "demo-co").first():
    print("Demo tenant already exists — nothing to do. (Delete slate.db to reseed from scratch.)")
else:
    tenant = models.Tenant(name="Demo Co", slug="demo-co")
    db.add(tenant)
    db.flush()

    user = models.User(
        tenant_id=tenant.id,
        email="demo@slate.dev",
        hashed_password=hash_password("demo1234"),
        name="Demo User",
        role="org_admin",
        email_verified=True,
    )
    db.add(user)
    db.flush()

    # Two more teammates so RBAC is visible immediately, not just in theory.
    db.add(models.User(
        tenant_id=tenant.id, email="manager@slate.dev",
        hashed_password=hash_password("demo1234"), name="Morgan Reyes", role="manager",
        email_verified=True,
    ))
    db.add(models.User(
        tenant_id=tenant.id, email="viewer@slate.dev",
        hashed_password=hash_password("demo1234"), name="Alex Chen", role="read_only",
        email_verified=True,
    ))
    db.flush()

    won_deal = None
    for company, name, value, email, transitions in DEALS:
        party = models.Party(
            tenant_id=tenant.id, party_type="organization",
            display_name=company, primary_email=email, owner_user_id=user.id,
        )
        db.add(party)
        db.flush()

        created_at = days_ago(transitions[0][1])
        final_stage = transitions[-1][0]
        deal = models.Deal(
            tenant_id=tenant.id, party_id=party.id, name=name, value=value,
            stage=final_stage, owner_user_id=user.id, created_at=created_at,
            updated_at=days_ago(transitions[-1][1]),
        )
        db.add(deal)
        db.flush()

        prev_stage = None
        for stage, d_ago in transitions:
            db.add(models.DealStageHistory(
                tenant_id=tenant.id, deal_id=deal.id, from_stage=prev_stage,
                to_stage=stage, changed_at=days_ago(d_ago),
            ))
            prev_stage = stage

        if final_stage == "Won":
            won_deal = deal

    # A paid invoice for the one Won deal, so Finance and Analytics both
    # have real revenue to show instead of an empty state.
    if won_deal:
        invoice = models.Invoice(
            tenant_id=tenant.id, party_id=won_deal.party_id, deal_id=won_deal.id,
            invoice_number="INV-0001", description=won_deal.name, amount=won_deal.value,
            status="Paid", issued_at=days_ago(9), due_date=days_ago(-21), paid_at=days_ago(3),
        )
        db.add(invoice)

    ITEMS = [
        ("SKU-1001", "Barcode Scanner - Handheld", "unit", 42, 15, 89.00),
        ("SKU-1002", "Shipping Labels (roll of 500)", "roll", 8, 20, 12.50),
        ("SKU-1003", "Warehouse Tote Bin - Large", "unit", 130, 30, 6.75),
    ]
    for sku, name, uom, qty, reorder, cost in ITEMS:
        item = models.InventoryItem(
            tenant_id=tenant.id, sku=sku, name=name, unit_of_measure=uom,
            quantity_on_hand=qty, reorder_point=reorder, unit_cost=cost,
        )
        db.add(item)
        db.flush()
        db.add(models.InventoryMovement(
            tenant_id=tenant.id, item_id=item.id, movement_type="in",
            quantity=qty, note="Initial stock", occurred_at=days_ago(30),
        ))

    # Small org chart: a director with two reports.
    director_party = models.Party(tenant_id=tenant.id, party_type="person", display_name="Renata Voss", primary_email="renata@demo-co.com", owner_user_id=user.id)
    db.add(director_party)
    db.flush()
    director = models.Employee(
        tenant_id=tenant.id, party_id=director_party.id, employee_number="EMP-001",
        department="Operations", job_title="Director of Operations", hire_date=days_ago(400),
    )
    db.add(director)
    db.flush()

    for num, name, email, title, dept, hired in [
        ("EMP-002", "Wes Okafor", "wes@demo-co.com", "Operations Analyst", "Operations", 200),
        ("EMP-003", "Lina Torres", "lina@demo-co.com", "Warehouse Lead", "Operations", 120),
        ("EMP-004", "Morgan Reyes", "manager@slate.dev", "Sales Manager", "Sales", 300),
    ]:
        p = models.Party(tenant_id=tenant.id, party_type="person", display_name=name, primary_email=email, owner_user_id=user.id)
        db.add(p)
        db.flush()
        emp = models.Employee(
            tenant_id=tenant.id, party_id=p.id, employee_number=num, department=dept,
            job_title=title, manager_id=director.id, hire_date=days_ago(hired),
        )
        db.add(emp)
        db.flush()
        if email == "manager@slate.dev":
            manager_user = db.query(models.User).filter(models.User.email == "manager@slate.dev").first()
            manager_user.employee_id = emp.id  # links the login account to its HRM record, for ABAC scoping

    for name, persona in [
        ("Research Analyst", "You are a sharp sales-ops research analyst. Given a question about accounts, "
         "deals, or the market, gather the relevant facts and lay them out clearly and concisely — no "
         "recommendations yet, just the grounded picture someone else will act on."),
        ("Draft Writer", "You are a concise business writer. Turn analysis or bullet points into a short, "
         "professional draft — an email, a follow-up note, or a brief — in a direct, confident tone with "
         "no filler and no corporate speak."),
    ]:
        db.add(models.Agent(tenant_id=tenant.id, name=name, persona_prompt=persona, created_by_user_id=user.id))

    db.commit()
    print("Seeded. Log in with: demo@slate.dev / demo1234")

db.close()
