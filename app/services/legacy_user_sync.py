"""官网 ↔ 旧项目用户双向同步服务。

数据方向与归属：
- 上行（官网 → 旧项目）：注册成功后实时推送；失败写入本地 outbox（回落存储），
  由 6 小时对账任务重放，避免静默丢数据。
- 下行（旧项目 → 官网）：旧项目自建用户回推 + 增量拉取（按 ``updated_at`` 水位）；
  以 ``email`` 查重关联既有账号或创建，不生成占位邮箱。

幂等与冲突：以 ``sync_id`` 优先，其次 ``username`` / ``email``；同 sync_id 冲突以官网为准，
旧项目仅补齐官网缺失字段；密码两侧同为 bcrypt，官网为主时复制官网哈希，回推用户沿用其旧项目哈希。
"""

from __future__ import annotations

import secrets
from typing import Optional
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import ValidationException
from app.core.events import event_bus
from app.core.loguru_logger import get_logger
from app.core.security import async_get_password_hash
from app.core.timezone import now_utc
from app.models.legacy_sync import LegacySyncOutbox, LegacySyncState
from app.models.user import User
from app.repositories.user_repo import UserRepository
from app.schemas.user import InternalUserUpsertIn
from app.services.auth.auth_service import derive_username
from app.services.legacy_task_client import legacy_task_client

logger = get_logger("services.legacy_user_sync")

# 增量拉取水位线在 LegacySyncState 中的 key。
_WATERMARK_KEY = "legacy_user_watermark"
# 对账任务单点锁：多实例部署下由 PG advisory lock 保证同一时刻仅一轮对账。
_RECONCILE_LOCK_KEY = 662514087


def build_payload(user: User) -> dict:
    """官网用户 → 旧项目 /api/internal/users/upsert 入参。"""
    return {
        "syncId": user.sync_id,
        "username": user.username,
        "email": user.email,
        "nickname": user.display_name,
        "avatarUrl": user.avatar_url,
        "passwordHash": user.hashed_password,
        "active": user.is_active,
        # 账号资料字段：官网为准，旧项目缺失时补齐
        "fullName": user.full_name,
        "bio": user.bio,
        "githubUrl": user.github_url,
        "websiteUrl": user.website_url,
        # 官网 tech_tags 为数组，旧项目存逗号分隔字符串
        "techTags": ",".join(user.tech_tags) if user.tech_tags else None,
    }


def _user_view(user: User) -> dict:
    """同步接口返回的用户视图（不含密码等敏感字段）。"""
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "display_name": user.display_name,
        "avatar_url": user.avatar_url,
        "is_active": user.is_active,
        "sync_id": user.sync_id,
    }


class LegacyUserSync:
    """用户同步服务（请求级会话）。"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.user_repo = UserRepository(db)

    # -------------------------------------------------------------- 上行推送

    async def ensure_sync_id(self, user: User) -> str:
        """确保用户具备同步标识（缺失则生成 UUID hex 并落库）。"""
        if user.sync_id:
            return user.sync_id
        user.sync_id = uuid4().hex
        user.updated_at = now_utc()
        await self.db.commit()
        return user.sync_id

    async def push_user(self, user: User) -> bool:
        """实时推送用户到旧项目；失败写 outbox，不抛异常（失败静默）。"""
        sync_id = await self.ensure_sync_id(user)
        payload = build_payload(user)
        try:
            await legacy_task_client.upsert_user(payload)
        except Exception as exc:  # noqa: BLE001 - 同步失败不阻断主流程
            logger.warning("用户推送旧项目失败，写入 outbox", sync_id=sync_id, error=str(exc))
            await self._enqueue_outbox(sync_id, payload, exc)
            return False
        await self._clear_outbox(sync_id)
        return True

    async def _enqueue_outbox(self, sync_id: str, payload: dict, error: Exception) -> None:
        """写入/更新 outbox 记录（同 sync_id 幂等覆盖）。"""
        existing = (
            await self.db.execute(
                select(LegacySyncOutbox).where(
                    LegacySyncOutbox.entity == "user",
                    LegacySyncOutbox.sync_id == sync_id,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            self.db.add(
                LegacySyncOutbox(
                    entity="user",
                    sync_id=sync_id,
                    operation="upsert",
                    payload=payload,
                    attempts=1,
                    last_error=str(error)[:2000],
                )
            )
        else:
            existing.payload = payload
            existing.attempts += 1
            existing.last_error = str(error)[:2000]
        await self.db.commit()

    async def _clear_outbox(self, sync_id: str) -> None:
        row = (
            await self.db.execute(
                select(LegacySyncOutbox).where(
                    LegacySyncOutbox.entity == "user",
                    LegacySyncOutbox.sync_id == sync_id,
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            await self.db.delete(row)
            await self.db.commit()

    # -------------------------------------------------------------- 下行回推

    async def handle_inbound_upsert(self, data: InternalUserUpsertIn) -> dict:
        """接收旧项目回推：按 email 查重，命中关联既有账号，未命中创建。"""
        email = data.email.strip().lower()
        user = await self._find_existing(data, email)

        if user is not None:
            await self._link_existing(user, data)
            await self.db.commit()
            await self.db.refresh(user)
            return _user_view(user)

        username = await self._unique_username(data.username or derive_username(email))
        password_hash = data.password_hash or await async_get_password_hash(
            secrets.token_hex(16)
        )
        user = await self.user_repo.create(
            {
                "username": username,
                "email": email,
                "hashed_password": password_hash,
                "display_name": data.nickname,
                "avatar_url": data.avatar_url,
                "is_active": True if data.active is None else data.active,
                "is_superuser": False,
                "sync_id": data.sync_id or uuid4().hex,
            }
        )
        await self.db.commit()
        await self.db.refresh(user)
        return _user_view(user)

    async def _find_existing(
        self, data: InternalUserUpsertIn, email: str
    ) -> Optional[User]:
        """按 sync_id → username → email 顺序匹配既有账号。"""
        if data.sync_id:
            found = (
                await self.db.execute(
                    select(User).where(
                        User.sync_id == data.sync_id, User.deleted_at.is_(None)
                    )
                )
            ).scalar_one_or_none()
            if found is not None:
                return found
        if data.username:
            found = await self.user_repo.get_by_username(data.username)
            if found is not None:
                return found
        return await self.user_repo.get_by_email(email)

    async def _link_existing(self, user: User, data: InternalUserUpsertIn) -> None:
        """关联既有账号：补齐 sync_id 与官网缺失字段，不覆盖密码。"""
        if not user.sync_id and data.sync_id:
            user.sync_id = data.sync_id
        if not user.display_name and data.nickname:
            user.display_name = data.nickname
        if not user.avatar_url and data.avatar_url:
            user.avatar_url = data.avatar_url
        if data.active is not None and user.is_active != data.active:
            user.is_active = data.active
        user.updated_at = now_utc()

    async def _unique_username(self, base: str) -> str:
        username = base
        for suffix in range(1, 100):
            if not await self.user_repo.get_by_username(username):
                return username
            username = f"{base[:40]}_{suffix}"
        return f"{base[:30]}_{uuid4().hex[:8]}"

    # -------------------------------------------------------------- 对账

    async def reconcile(self) -> dict:
        """增量对账：重放 outbox + 拉取旧项目变更用户（水位线推进）。"""
        replayed = await self._replay_outbox()
        pulled, watermark = await self._pull_legacy_changes()
        if watermark is not None:
            await self._set_watermark(watermark)
        return {"replayed": replayed, "pulled": pulled}

    async def _replay_outbox(self) -> int:
        rows = (
            (await self.db.execute(select(LegacySyncOutbox).order_by(LegacySyncOutbox.id)))
            .scalars()
            .all()
        )
        replayed = 0
        for row in rows:
            try:
                await legacy_task_client.upsert_user(row.payload)
            except Exception as exc:  # noqa: BLE001 - 保留待下次重放
                row.attempts += 1
                row.last_error = str(exc)[:2000]
                continue
            await self.db.delete(row)
            replayed += 1
        await self.db.commit()
        return replayed

    async def _pull_legacy_changes(self) -> tuple[int, Optional[str]]:
        since = await self._get_watermark()
        changes = await legacy_task_client.fetch_user_changes(since)
        if not changes:
            return 0, None

        pulled = 0
        max_watermark = since
        for item in changes:
            payload = InternalUserUpsertIn(
                sync_id=item.get("syncId"),
                username=item.get("username"),
                email=item.get("email") or "",
                nickname=item.get("nickname"),
                avatar_url=item.get("avatar"),
                password_hash=None,
                active=_to_active(item.get("status")),
            )
            if not payload.email:
                # 旧项目存量无邮箱用户：跳过（两侧统一后不应再出现）
                logger.warning("旧项目变更用户缺少邮箱，跳过", sync_id=item.get("syncId"))
                continue
            await self.handle_inbound_upsert(payload)
            pulled += 1
            updated = item.get("updateTime")
            if updated and (max_watermark is None or str(updated) > max_watermark):
                max_watermark = str(updated)

        return pulled, max_watermark

    async def _get_watermark(self) -> Optional[str]:
        row = (
            await self.db.execute(
                select(LegacySyncState).where(LegacySyncState.key == _WATERMARK_KEY)
            )
        ).scalar_one_or_none()
        return row.value if row else None

    async def _set_watermark(self, value: str) -> None:
        row = (
            await self.db.execute(
                select(LegacySyncState).where(LegacySyncState.key == _WATERMARK_KEY)
            )
        ).scalar_one_or_none()
        if row is None:
            self.db.add(LegacySyncState(key=_WATERMARK_KEY, value=value))
        else:
            row.value = value
            row.updated_at = now_utc()
        await self.db.commit()


def _to_active(status: object) -> Optional[bool]:
    """旧项目 status（1/0）→ 官网 is_active；缺失返回 None（不覆盖）。"""
    if status is None:
        return None
    try:
        return int(status) == 1
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ 后台入口


async def push_user_by_id(user_id: int) -> None:
    """注册事件订阅者：以独立会话推送用户（失败静默，仅落 outbox）。"""
    from app.database import get_session

    async with get_session() as db:
        user = await db.get(User, user_id)
        if user is None or user.deleted_at is not None:
            return
        await LegacyUserSync(db).push_user(user)


async def reconcile_once() -> dict:
    """6 小时对账入口：advisory lock 保证多实例单点执行。"""
    from app.database import get_session

    if not settings.legacy_enabled:
        return {"skipped": True}

    async with get_session() as db:
        lock_acquired = await db.scalar(
            text("SELECT pg_try_advisory_xact_lock(:lock_key)"),
            {"lock_key": _RECONCILE_LOCK_KEY},
        )
        if not lock_acquired:
            return {"skipped": True}
        return await LegacyUserSync(db).reconcile()


def register_legacy_sync_events() -> None:
    """注册用户同步事件订阅（幂等）。"""
    event_bus.subscribe("user.registered", _on_user_registered)
    logger.debug("旧项目用户同步订阅已注册")


async def _on_user_registered(user_id: int) -> None:
    await push_user_by_id(user_id)