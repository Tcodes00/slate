from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import models
import schemas
from auth import get_current_user, require_role
from database import get_db

router = APIRouter(prefix="/api/projects", tags=["projects"])


def _project_out(db: Session, p: models.Project) -> dict:
    tasks = db.query(models.Task).filter(models.Task.project_id == p.id).all()
    return {
        "id": p.id, "name": p.name, "status": p.status, "budget": p.budget,
        "task_count": len(tasks), "done_count": sum(1 for t in tasks if t.status == "Done"),
    }


@router.get("", response_model=list[schemas.ProjectOut])
def list_projects(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    projects = db.query(models.Project).filter(models.Project.tenant_id == user.tenant_id).all()
    return [_project_out(db, p) for p in projects]


@router.post("", response_model=schemas.ProjectOut)
def create_project(payload: schemas.ProjectCreate, db: Session = Depends(get_db), user: models.User = Depends(require_role("standard_user"))):
    project = models.Project(tenant_id=user.tenant_id, name=payload.name, budget=payload.budget)
    db.add(project)
    db.commit()
    db.refresh(project)
    return _project_out(db, project)


@router.get("/{project_id}/tasks", response_model=list[schemas.TaskOut])
def list_tasks(project_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    return db.query(models.Task).filter(models.Task.project_id == project_id, models.Task.tenant_id == user.tenant_id).all()


@router.post("/{project_id}/tasks", response_model=schemas.TaskOut)
def create_task(project_id: int, payload: schemas.TaskCreate, db: Session = Depends(get_db), user: models.User = Depends(require_role("standard_user"))):
    project = db.query(models.Project).filter(models.Project.id == project_id, models.Project.tenant_id == user.tenant_id).first()
    if not project:
        raise HTTPException(404, "Project not found")
    task = models.Task(tenant_id=user.tenant_id, project_id=project_id, title=payload.title, due_date=payload.due_date, assigned_to_user_id=user.id)
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


@router.patch("/tasks/{task_id}", response_model=schemas.TaskOut)
def update_task(task_id: int, payload: schemas.TaskUpdate, db: Session = Depends(get_db), user: models.User = Depends(require_role("standard_user"))):
    task = db.query(models.Task).filter(models.Task.id == task_id, models.Task.tenant_id == user.tenant_id).first()
    if not task:
        raise HTTPException(404, "Task not found")
    task.status = payload.status
    db.commit()
    db.refresh(task)
    return task
