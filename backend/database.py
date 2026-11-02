"""
Database connection setup.

Defaults to a local SQLite file so this runs with zero setup — no Postgres
server required to try it. Swap to Postgres for real deployment by setting
DATABASE_URL, e.g.:
    DATABASE_URL=postgresql://user:password@localhost:5432/slate
Nothing else in the codebase needs to change; SQLAlchemy abstracts the engine.
"""

import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./slate.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
