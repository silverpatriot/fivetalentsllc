import enum
import uuid

from sqlalchemy import Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UUIDPkMixin


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    PASTOR = "pastor"
    EDITOR = "editor"
    VIEWER = "viewer"


class User(Base, UUIDPkMixin, CreatedAtMixin):
    __tablename__ = "users"

    clerk_user_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role", native_enum=True),
        nullable=False,
        server_default=UserRole.VIEWER.value,
    )
