from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import models
import schemas
from auth import get_current_user, require_role
from database import get_db

router = APIRouter(prefix="/api/hrm", tags=["hrm"])


def _employee_out(db: Session, emp: models.Employee) -> dict:
    manager_name = None
    if emp.manager_id:
        mgr = db.query(models.Employee).filter(models.Employee.id == emp.manager_id).first()
        if mgr:
            manager_name = mgr.party.display_name if mgr.party else None
    return {
        "id": emp.id,
        "name": emp.party.display_name if emp.party else "",
        "email": emp.party.primary_email if emp.party else None,
        "employee_number": emp.employee_number,
        "department": emp.department,
        "job_title": emp.job_title,
        "manager_id": emp.manager_id,
        "manager_name": manager_name,
        "status": emp.status,
    }


def _leave_out(db: Session, lr: models.LeaveRequest) -> dict:
    emp = db.query(models.Employee).filter(models.Employee.id == lr.employee_id).first()
    return {
        "id": lr.id,
        "employee_id": lr.employee_id,
        "employee_name": emp.party.display_name if emp and emp.party else "Unknown",
        "leave_type": lr.leave_type,
        "start_date": lr.start_date,
        "end_date": lr.end_date,
        "status": lr.status,
        "reason": lr.reason,
    }


@router.get("/employees", response_model=list[schemas.EmployeeOut])
def list_employees(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    query = db.query(models.Employee).filter(models.Employee.tenant_id == user.tenant_id)

    # ABAC, not just RBAC: a Manager's role alone doesn't grant company-wide
    # visibility — it's scoped to their own department, unless they're an
    # Org Admin. This is an attribute condition (department match), checked
    # server-side, not a UI-level filter that a direct API call could skip.
    if user.role == "org_admin":
        pass  # full company visibility
    elif user.role == "manager" and user.employee_id:
        self_record = db.query(models.Employee).filter(models.Employee.id == user.employee_id).first()
        if self_record and self_record.department:
            query = query.filter(models.Employee.department == self_record.department)
    elif user.employee_id:
        query = query.filter(models.Employee.id == user.employee_id)
    else:
        return []

    employees = query.all()
    return [_employee_out(db, e) for e in employees]


@router.post("/employees/{employee_id}/link-me")
def link_self_to_employee(employee_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    """Connects the logged-in account to its HRM record — what the ABAC
    department check above and 'my own record only' scoping actually key
    off. An Org Admin does this once per teammate after adding them."""
    emp = db.query(models.Employee).filter(models.Employee.id == employee_id, models.Employee.tenant_id == user.tenant_id).first()
    if not emp:
        raise HTTPException(404, "Employee not found")
    user.employee_id = employee_id
    db.commit()
    return {"message": f"Linked to employee record: {emp.party.display_name if emp.party else employee_id}"}


@router.post("/employees", response_model=schemas.EmployeeOut)
def create_employee(
    payload: schemas.EmployeeCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("manager")),
):
    party = models.Party(
        tenant_id=user.tenant_id, party_type="person", display_name=payload.name,
        primary_email=payload.email, owner_user_id=user.id,
    )
    db.add(party)
    db.flush()

    employee = models.Employee(
        tenant_id=user.tenant_id, party_id=party.id, employee_number=payload.employee_number,
        department=payload.department, job_title=payload.job_title,
        manager_id=payload.manager_id, hire_date=payload.hire_date,
    )
    db.add(employee)
    db.commit()
    db.refresh(employee)
    return _employee_out(db, employee)


@router.patch("/employees/{employee_id}", response_model=schemas.EmployeeOut)
def update_employee(
    employee_id: int,
    payload: schemas.EmployeeUpdate,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("manager")),
):
    emp = db.query(models.Employee).filter(
        models.Employee.id == employee_id, models.Employee.tenant_id == user.tenant_id
    ).first()
    if not emp:
        raise HTTPException(404, "Employee not found")
    if payload.department is not None:
        emp.department = payload.department
    if payload.job_title is not None:
        emp.job_title = payload.job_title
    if payload.manager_id is not None:
        emp.manager_id = payload.manager_id
    if payload.status is not None:
        emp.status = payload.status
    db.commit()
    db.refresh(emp)
    return _employee_out(db, emp)


@router.get("/leave-requests", response_model=list[schemas.LeaveRequestOut])
def list_leave_requests(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    requests = db.query(models.LeaveRequest).filter(models.LeaveRequest.tenant_id == user.tenant_id).all()
    return [_leave_out(db, r) for r in requests]


@router.post("/leave-requests", response_model=schemas.LeaveRequestOut)
def create_leave_request(
    payload: schemas.LeaveRequestCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("standard_user")),
):
    emp = db.query(models.Employee).filter(
        models.Employee.id == payload.employee_id, models.Employee.tenant_id == user.tenant_id
    ).first()
    if not emp:
        raise HTTPException(404, "Employee not found")

    # ABAC, same boundary list_employees already enforces for reads, now
    # enforced for this write too: everyone below Org Admin can only file
    # leave against their own linked record, not any employee in the
    # company just because they know the ID.
    if user.role != "org_admin" and payload.employee_id != user.employee_id:
        raise HTTPException(403, "You can only submit a leave request for your own employee record")

    leave = models.LeaveRequest(
        tenant_id=user.tenant_id, employee_id=payload.employee_id, leave_type=payload.leave_type,
        start_date=payload.start_date, end_date=payload.end_date, reason=payload.reason,
    )
    db.add(leave)
    db.commit()
    db.refresh(leave)
    return _leave_out(db, leave)


@router.patch("/leave-requests/{request_id}", response_model=schemas.LeaveRequestOut)
def update_leave_request(
    request_id: int,
    payload: schemas.LeaveRequestUpdate,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("manager")),
):
    leave = db.query(models.LeaveRequest).filter(
        models.LeaveRequest.id == request_id, models.LeaveRequest.tenant_id == user.tenant_id
    ).first()
    if not leave:
        raise HTTPException(404, "Leave request not found")
    leave.status = payload.status
    db.commit()
    db.refresh(leave)
    return _leave_out(db, leave)
