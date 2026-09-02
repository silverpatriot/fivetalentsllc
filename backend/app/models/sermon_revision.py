import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UUIDPkMixin


class SermonRevision(Base, UUIDPkMixin, CreatedAtMixin):
    """A snapshot of sermons.content taken immediately BEFORE an edit
    overwrites it — migration 0015's minimum-viable recoverability (Phase
    6 Task 2): not a full version-history feature (no revert endpoint,
    no UI reads this yet), just enough that an edit is never a one-way
    door at the data layer. Reading a sermon's rows in created_at order,
    ending at the sermon's own current `content`, reconstructs the full
    lineage.

    `instruction` is the edit instruction that caused THIS row's content
    to be superseded — kept alongside the snapshot so a future revision-
    history UI can show "what changed and why" without a second table.
    """

    __tablename__ = "sermon_revisions"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sermon_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sermons.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
