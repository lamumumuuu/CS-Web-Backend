"""旧项目外接同步支撑表：待同步 outbox 与增量对账水位线。

任务数据不落库（唯一事实源在旧项目 tasks 表）；本文件只承载**用户同步**的可靠性支撑：
- LegacySyncOutbox：推送旧项目失败时的回落存储，定时任务按水位线重放，避免静默丢数据。
- LegacySyncState：按 key 记录增量对账水位线（如最近一次拉取到的 updated_at）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime as _DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.timezone import now_utc
from app.database import Base
from app.models.types import JSONDict

DateTime = _DateTime(timezone=True)


class LegacySyncOutbox(Base):
    """待同步（outbox）：官网用户变更推送旧项目失败后的回落记录。

    幂等：同一 (entity, sync_id) 只保留一条；重放成功即删除。
    重放由 6 小时对账任务触发，不引入额外队列中间件。
    """

    __tablename__ = "legacy_sync_outbox"
    __table_args__ = (
        UniqueConstraint("entity", "sync_id", name="ux_legacy_sync_outbox_entity_sync"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 目标实体：当前仅 "user"，预留扩展。
    entity: Mapped[str] = mapped_column(String(30), nullable=False, default="user")
    # 幂等键：官网用户 sync_id。
    sync_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # 操作类型：当前仅 upsert。
    operation: Mapped[str] = mapped_column(String(20), nullable=False, default="upsert")
    # 待推送载荷（与旧项目 /api/internal/users/upsert 入参一致）。
    payload: Mapped[dict] = mapped_column(JSONDict, nullable=False)
    # 重试次数与最近一次错误，便于排障与死信识别。
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_utc, onupdate=now_utc
    )

    def __repr__(self) -> str:
        return (
            f"<LegacySyncOutbox(id={self.id}, entity='{self.entity}', "
            f"sync_id='{self.sync_id}')>"
        )


class LegacySyncState(Base):
    """同步运行态：按 key 记录水位线等单值状态（键值表）。"""

    __tablename__ = "legacy_sync_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(50), nullable=False, unique=True, index=True)
    value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_utc, onupdate=now_utc
    )

    def __repr__(self) -> str:
        return f"<LegacySyncState(key='{self.key}')>"