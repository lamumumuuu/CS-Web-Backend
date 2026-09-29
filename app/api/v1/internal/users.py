"""官网后端内部接口：接收旧项目回推的用户。

来源校验：共享密钥 ``INTERNAL_TOKEN``（请求头 ``X-Internal-Token``）。
未配置密钥时 fail-closed（返回 503），避免内部通道在未设防时被外部滥用。
本期旧项目回推走此通道；下次更新官网出站调用亦切换为同一共享密钥。
"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Depends, Header

from app.core.config import settings
from app.core.exceptions import AuthorizationException, ExternalServiceException
from app.dependencies_services import get_legacy_user_sync
from app.schemas.user import InternalUserUpsertIn
from app.services.legacy_user_sync import LegacyUserSync

router = APIRouter()


def require_internal_token(
    x_internal_token: str | None = Header(default=None),
) -> None:
    """校验内部共享密钥；未配置 fail-closed，配置不符返回 403。"""
    expected = settings.INTERNAL_TOKEN
    if not expected:
        raise ExternalServiceException(
            message="内部回推通道未配置",
            service_name="internal",
            status_code=503,
        )
    if not x_internal_token or not secrets.compare_digest(x_internal_token, expected):
        raise AuthorizationException(message="内部凭证校验失败")


@router.post("/users/upsert", dependencies=[Depends(require_internal_token)])
async def upsert_user(
    body: InternalUserUpsertIn,
    service: LegacyUserSync = Depends(get_legacy_user_sync),
) -> Any:
    """旧项目回推用户：按 email 查重关联既有账号或创建（不生成占位邮箱）。"""
    return await service.handle_inbound_upsert(body)