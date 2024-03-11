import os
from pathlib import Path
from uuid import uuid4

import lancedb
import pyarrow as pa
import tantivy
from pydantic import computed_field
from slugify import slugify
from sqlmodel import Field, Relationship, SQLModel, create_engine

from tracecat import auth

STORAGE_PATH = Path(os.path.expanduser("~/.tracecat/storage"))
EMBEDDINGS_SIZE = os.environ.get("TRACECAT__EMBEDDINGS_SIZE", 512)


class User(SQLModel, table=True):
    id: str | None = Field(default_factory=lambda: uuid4().hex, primary_key=True)
    tier: str = "free"  # "free" or "premium"
    settings: str | None = None  # JSON-serialized String of settings
    owned_workflows: list["Workflow"] = Relationship(back_populates="owner")


class Editor(SQLModel, table=True):
    user_id: str | None = Field(default=None, foreign_key="user.id", primary_key=True)
    workflow_id: str | None = Field(
        default=None, foreign_key="workflow.id", primary_key=True
    )


class Workflow(SQLModel, table=True):
    id: str | None = Field(default_factory=lambda: uuid4().hex, primary_key=True)
    title: str
    description: str
    status: str = "offline"  # "online" or "offline"
    object: str | None = None  # JSON-serialized String of react flow object
    # Owner
    owner_id: str | None = Field(default=None, foreign_key="user.id")
    owner: User | None = Relationship(back_populates="owned_workflows")
    actions: list["Action"] | None = Relationship(back_populates="workflow")
    runs: list["WorkflowRun"] | None = Relationship(back_populates="workflow")
    webhooks: list["Webhook"] | None = Relationship(back_populates="workflow")


class WorkflowRun(SQLModel, table=True):
    id: str | None = Field(default_factory=lambda: uuid4().hex, primary_key=True)
    status: str = "pending"  # "online" or "offline"
    workflow_id: str = Field(foreign_key="workflow.id")
    workflow: Workflow | None = Relationship(back_populates="runs")


class Action(SQLModel, table=True):
    id: str | None = Field(default_factory=lambda: uuid4().hex, primary_key=True)
    type: str
    title: str
    description: str
    status: str = "offline"  # "online" or "offline"
    inputs: str | None = None  # JSON-serialized String of inputs
    workflow_id: str | None = Field(foreign_key="workflow.id")
    workflow: Workflow | None = Relationship(back_populates="actions")

    @computed_field
    @property
    def action_key(self) -> str:
        slug = slugify(self.title, separator="_")
        return f"{self.id}.{slug}"


class Webhook(SQLModel, table=True):
    """Webhook is a URL that can be called to trigger a workflow.

    Notes
    -----
    - We need this because we need a way to trigger a workflow from an external source.
    - External sources only have access to the path
    """

    id: str | None = Field(
        default_factory=lambda: uuid4().hex,
        primary_key=True,
        description="Webhook path",
    )
    action_id: str | None = Field(foreign_key="action.id")
    workflow_id: str | None = Field(foreign_key="workflow.id")
    workflow: Workflow | None = Relationship(back_populates="webhooks")

    @computed_field
    @property
    def secret(self) -> str:
        return auth.compute_hash(self.id)


def create_db_engine():
    STORAGE_PATH.mkdir(parents=True, exist_ok=True)
    sqlite_uri = f"sqlite:////{STORAGE_PATH}/database.db"
    engine = create_engine(
        sqlite_uri, echo=True, connect_args={"check_same_thread": False}
    )
    return engine


def build_events_index():
    index_path = STORAGE_PATH / "event_index"
    index_path.mkdir(parents=True, exist_ok=True)
    event_schema = (
        tantivy.SchemaBuilder()
        .add_date_field("published_at", fast=True, stored=True)
        .add_text_field("action_id", stored=True)
        .add_text_field("action_run_id", stored=True)
        .add_text_field("action_title", stored=True)
        .add_text_field("action_type", stored=True)
        .add_text_field("workflow_id", stored=True)
        .add_text_field("workflow_title", stored=True)
        .add_text_field("workflow_run_id", stored=True)
        .add_json_field("data", stored=True)
        .build()
    )
    tantivy.Index(event_schema, path=str(index_path))


def create_events_index() -> tantivy.Index:
    index_path = STORAGE_PATH / "event_index"
    return tantivy.Index.open(str(index_path))


def create_vdb_conn() -> lancedb.DBConnection:
    db = lancedb.connect(STORAGE_PATH / "vector.db")
    return db


CaseSchema = pa.schema(
    [
        pa.field("id", pa.int64(), nullable=False),
        pa.field("workflow_id", pa.int64(), nullable=False),
        pa.field("title", pa.string(), nullable=False),
        pa.field("payload", pa.string(), nullable=False),  # JSON-serialized
        pa.field("context", pa.string(), nullable=True),  # JSON-serialized
        pa.field("malice", pa.string(), nullable=False),
        pa.field("status", pa.string(), nullable=False),
        pa.field("priority", pa.string(), nullable=False),
        pa.field("action", pa.string(), nullable=True),
        pa.field("suppression", pa.string(), nullable=True),  # JSON-serialized
        # pa.field("_action_vector", pa.list_(pa.float32(), list_size=EMBEDDINGS_SIZE)),
        # pa.field("_payload_vector", pa.list_(pa.float32(), list_size=EMBEDDINGS_SIZE)),
        # pa.field("_context_vector", pa.list_(pa.float32(), list_size=EMBEDDINGS_SIZE)),
    ]
)


def initialize_db() -> None:
    # Relational table
    engine = create_db_engine()
    SQLModel.metadata.create_all(engine)

    # VectorDB
    db = create_vdb_conn()
    db.create_table("cases", schema=CaseSchema, exist_ok=True)

    # Search
    build_events_index()
