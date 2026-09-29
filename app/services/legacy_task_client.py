"""旧项目（FGS_CS_B）任务模块出站客户端：官网 BFF 代理层的唯一外部依赖。

职责边界：
- 拼接旧项目 BASE_URL，注入服务账号 JWT 与行为主体头 ``X-Acting-User-Id``；
  契约预留 ``X-Internal-Token``（下次更新以共享密钥替代服务账号 JWT，业务代码零改动）。
- 任务状态「中文 → 英文」单点映射（未知值透传 + 告警），字段映射为官网对外契约。
- 超时与重试（仅幂等 GET）、旧项目错误码 → 官网异常映射。
- 外接链路未配置时抛 503，由代理端点透出，不影响官网其它模块。

设计取舍：
- 任务数据的唯一事实源是旧项目 ``tasks`` 表，本客户端不做任何落库、不缓存。
- 状态映射表是本仓库唯一一份中文→英文映射（旧项目侧仅收敛常量，不对外输出英文）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import httpx
import jwt

from app.core.config import settings
from app.core.exceptions import (
    AuthenticationException,
    AuthorizationException,
    BusinessException,
    ConflictException,
    ExternalServiceException,
    NotFoundException,
    ValidationException,
)
from app.core.loguru_logger import get_logger
from app.core.timezone import now_utc

logger = get_logger("services.legacy_task_client")

# 幂等读操作的重试次数（首次 + 1 次重试）；写操作不重试，避免重复副作用。
_READ_ATTEMPTS = 2

# 任务状态单点映射：旧项目中文 label（数据库存储值）→ 官网英文 code。
_STATUS_LABEL_TO_CODE: dict[str, str] = {
    "审核中": "pending_review",
    "等待中": "published",
    "讨伐中": "in_progress",
    "已提交": "submitted",
    "已完成": "completed",
    "已驳回": "rejected",
    "已强制结项": "closed",
    "已结束": "expired",
}
_STATUS_CODE_TO_LABEL: dict[str, str] = {v: k for k, v in _STATUS_LABEL_TO_CODE.items()}

# 旧项目创建/更新任务的难度默认值（CreateTaskDTO.difficulty 为 @NotBlank）。
_DEFAULT_DIFFICULTY = "普通"


class LegacyNotConfiguredError(ExternalServiceException):
    """外接链路未配置（LEGACY_API_BASE_URL / LEGACY_JWT_SECRET 缺失）。

    单独成类便于代理端点识别并返回 503，而非笼统 502。
    """

    def __init__(self) -> None:
        super().__init__(
            message="旧项目外接链路未启用",
            service_name="FGS_CS_B",
            status_code=503,
        )


# ------------------------------------------------------------------ 映射工具


def map_status_to_official(label: Optional[str]) -> Optional[str]:
    """旧项目中文状态 → 官网英文状态；未知值透传并告警（不阻断展示）。"""
    if label is None:
        return None
    code = _STATUS_LABEL_TO_CODE.get(label)
    if code is None:
        logger.warning("旧项目任务状态未纳入映射，按原值透传", status=label)
        return label
    return code


def map_status_to_legacy(code: Optional[str]) -> Optional[str]:
    """官网英文状态 → 旧项目中文状态（列表筛选参数用）；未知值原样透传。"""
    if code is None:
        return None
    return _STATUS_CODE_TO_LABEL.get(code, code)


def _split_tech(value: Any) -> list[str]:
    """旧项目 techRequirements 为逗号分隔串，官网按数组消费。"""
    if not value:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in str(value).split(",") if part.strip()]


def _join_tech(value: Any) -> Optional[str]:
    """官网技术栈（数组）→ 旧项目逗号分隔串；旧项目该字段为 String，传数组会 500。"""
    parts = _split_tech(value)
    return ",".join(parts) if parts else None


def _completer(task: dict) -> Optional[dict]:
    """旧项目单接取人 → 官网合成对象 {id, name, avatar}。"""
    completer_id = task.get("completerId")
    if completer_id is None:
        return None
    return {
        "id": str(completer_id),
        "name": task.get("completerName"),
        "avatar": task.get("completerAvatar"),
    }


def map_task(task: dict) -> dict:
    """旧项目 Task → 官网对外任务契约（字段名与前端 BFF 约定一致）。"""
    publisher_id = task.get("publisherId")
    return {
        "id": str(task["id"]),
        "title": task.get("title"),
        "description": task.get("description"),
        "difficulty": task.get("difficulty"),
        "tech_requirements": _split_tech(task.get("techRequirements")),
        "reward": task.get("reward") or 0,
        "deadline": task.get("deadline"),
        "status": map_status_to_official(task.get("status")),
        # 原始中文状态：管理端审核与排障展示用，前端可只读展示。
        "status_label": task.get("status"),
        "publisher_id": str(publisher_id) if publisher_id is not None else None,
        "publisher_name": task.get("publisherName"),
        "publisher_avatar": task.get("publisherAvatar"),
        "completer": _completer(task),
        "submission_description": task.get("submissionDescription"),
        "attachment_url": task.get("attachmentUrl"),
        "reject_reason": task.get("rejectReason"),
        "reviewed_by": task.get("reviewedBy"),
        "reviewed_at": task.get("reviewedAt"),
        "created_at": task.get("createdAt"),
        "updated_at": task.get("updatedAt"),
    }


def map_task_input(data: dict) -> dict:
    """官网任务入参 → 旧项目 CreateTaskDTO。

    官网前端在阶段 3 起直接提交 difficulty / reward / deadline / tech_requirements；
    为兼容存量管理端仍提交 points / tags / category，此处做等价兜底。
    """
    title = (data.get("title") or "").strip()
    description = (data.get("description") or "").strip()
    reward = data.get("reward")
    if reward is None:
        reward = data.get("points")
    difficulty = (data.get("difficulty") or "").strip() or _DEFAULT_DIFFICULTY

    tech = data.get("tech_requirements")
    if tech is None:
        tech = data.get("tags")
    tech = _join_tech(tech)

    # deadline 可能来自 Pydantic 的 datetime，需转 ISO 串才能被 httpx 的 json 编码。
    deadline = data.get("deadline")
    if isinstance(deadline, datetime):
        deadline = deadline.isoformat()

    payload: dict[str, Any] = {
        "title": title,
        "description": description,
        "difficulty": difficulty,
        "reward": int(reward) if reward is not None else 1,
        "techRequirements": tech,
        "deadline": deadline,
        # 官网无「峰」概念，透传空值；旧项目按发布者归属峰处理。
        "peakId": data.get("peak_id"),
    }
    return payload


# ------------------------------------------------------------------ 客户端


def _service_token() -> str:
    """签发服务账号 JWT（HS256，与旧项目 jwt.secret 一致，claim username 命中即内部调用）。"""
    issued_at = int(now_utc().timestamp())
    payload = {
        "username": settings.LEGACY_SERVICE_ACCOUNT,
        "created": issued_at,
        "iat": issued_at,
        "exp": issued_at + int(settings.LEGACY_SERVICE_TOKEN_TTL_SECONDS),
    }
    return jwt.encode(payload, settings.LEGACY_JWT_SECRET or "", algorithm="HS256")


class LegacyTaskClient:
    """旧项目任务/用户内部接口的异步客户端（无状态，可安全复用）。"""

    def _ensure_enabled(self) -> None:
        if not settings.legacy_enabled:
            raise LegacyNotConfiguredError()

    def _headers(self, acting_sync_id: Optional[str]) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {_service_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            settings.LEGACY_SOURCE_HEADER: settings.LEGACY_SOURCE_VALUE,
        }
        # 契约预留：下次更新以共享密钥做密钥级校验，替换服务账号 JWT。
        if settings.INTERNAL_TOKEN:
            headers["X-Internal-Token"] = settings.INTERNAL_TOKEN
        if acting_sync_id:
            headers[settings.LEGACY_ACTING_HEADER] = acting_sync_id
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        acting_sync_id: Optional[str] = None,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
    ) -> Any:
        self._ensure_enabled()
        base = (settings.LEGACY_API_BASE_URL or "").rstrip("/")
        url = f"{base}{path}"
        headers = self._headers(acting_sync_id)
        attempts = _READ_ATTEMPTS if method.upper() == "GET" else 1
        last_error: Optional[Exception] = None

        for attempt in range(attempts):
            try:
                async with httpx.AsyncClient(
                    timeout=settings.LEGACY_TIMEOUT_SECONDS
                ) as client:
                    resp = await client.request(
                        method, url, params=params, json=json_body, headers=headers
                    )
                return self._unwrap(resp, path)
            except httpx.HTTPError as exc:
                last_error = exc
                logger.warning(
                    "旧项目请求失败",
                    method=method,
                    path=path,
                    attempt=attempt + 1,
                    error=str(exc),
                )

        raise ExternalServiceException(
            message="旧项目不可达",
            service_name="FGS_CS_B",
            endpoint=path,
            cause=last_error,
        )

    @staticmethod
    def _unwrap(resp: httpx.Response, path: str) -> Any:
        """解析旧项目统一响应体 {code, message, data}，并做错误码映射。"""
        # Spring Security 层的 401/403 可能不是 Result 结构，先按 HTTP 状态判定。
        if resp.status_code == 401:
            raise AuthenticationException(message="旧项目认证失败，请重新登录")
        if resp.status_code == 403:
            raise AuthorizationException(message="旧项目权限不足")
        if resp.status_code >= 500:
            raise ExternalServiceException(
                message="旧项目服务异常",
                service_name="FGS_CS_B",
                endpoint=path,
                details={"upstream_status": resp.status_code},
            )

        try:
            body = resp.json()
        except ValueError as exc:
            raise ExternalServiceException(
                message="旧项目返回非 JSON 响应",
                service_name="FGS_CS_B",
                endpoint=path,
                cause=exc,
            ) from exc

        if not isinstance(body, dict) or "code" not in body:
            raise ExternalServiceException(
                message="旧项目响应结构异常",
                service_name="FGS_CS_B",
                endpoint=path,
            )

        code = body.get("code")
        message = body.get("message") or "旧项目操作失败"
        if code == 200:
            return body.get("data")
        raise LegacyTaskClient._map_business_error(code, message)

    @staticmethod
    def _map_business_error(code: Any, message: str) -> Exception:
        """旧项目业务码 → 官网异常（旧项目沿用 HTTP 语义码）。"""
        if code == 400:
            return ValidationException(message=message)
        if code == 401:
            return AuthenticationException(message=message)
        if code == 403:
            return AuthorizationException(message=message)
        if code == 404:
            return NotFoundException(message=message)
        # 旧项目以 500 表达业务状态冲突（HTTP 仍为 200，如「任务状态错误」），
        # 映射为 409 以保留用户可读文案；真实系统异常已在上面按 HTTP 500 拦截。
        if code == 500:
            return ConflictException(message=message)
        return BusinessException(message=message)

    # -------------------------------------------------------------- 任务读

    async def list_tasks(
        self,
        *,
        acting_sync_id: Optional[str] = None,
        difficulty: Optional[str] = None,
        status: Optional[str] = None,
        keyword: Optional[str] = None,
    ) -> list[dict]:
        """任务大厅列表（内部调用放开峰过滤，返回全量已发布任务）。"""
        params: dict[str, Any] = {}
        if difficulty:
            params["difficulty"] = difficulty
        if status:
            params["status"] = map_status_to_legacy(status)
        if keyword:
            params["keyword"] = keyword
        data = await self._request(
            "GET",
            "/api/tasks",
            acting_sync_id=acting_sync_id,
            params=params or None,
        )
        return [map_task(t) for t in (data or [])]

    async def get_task(
        self, task_id: str, *, acting_sync_id: Optional[str] = None
    ) -> dict:
        data = await self._request(
            "GET", f"/api/tasks/{task_id}", acting_sync_id=acting_sync_id
        )
        return map_task(data)

    async def my_accepted(self, *, acting_sync_id: str) -> list[dict]:
        data = await self._request(
            "GET", "/api/tasks/accepted", acting_sync_id=acting_sync_id
        )
        return [map_task(t) for t in (data or [])]

    async def my_completed(self, *, acting_sync_id: str) -> list[dict]:
        data = await self._request(
            "GET", "/api/tasks/completed", acting_sync_id=acting_sync_id
        )
        return [map_task(t) for t in (data or [])]

    async def my_published(self, *, acting_sync_id: str) -> list[dict]:
        data = await self._request(
            "GET", "/api/tasks/my", acting_sync_id=acting_sync_id
        )
        return [map_task(t) for t in (data or [])]

    async def pending_tasks(self, *, acting_sync_id: str) -> list[dict]:
        """待审核任务（管理端）。"""
        data = await self._request(
            "GET", "/api/tasks/pending", acting_sync_id=acting_sync_id
        )
        return [map_task(t) for t in (data or [])]

    # -------------------------------------------------------------- 任务写

    async def create_task(self, *, acting_sync_id: str, data: dict) -> dict:
        payload = map_task_input(data)
        result = await self._request(
            "POST", "/api/tasks", acting_sync_id=acting_sync_id, json_body=payload
        )
        return map_task(result)

    async def update_task(
        self, task_id: str, *, acting_sync_id: str, data: dict
    ) -> dict:
        payload = map_task_input(data)
        result = await self._request(
            "PUT",
            f"/api/tasks/{task_id}",
            acting_sync_id=acting_sync_id,
            json_body=payload,
        )
        return map_task(result)

    async def delete_task(self, task_id: str, *, acting_sync_id: str) -> None:
        await self._request(
            "DELETE", f"/api/tasks/{task_id}", acting_sync_id=acting_sync_id
        )

    async def accept_task(self, task_id: str, *, acting_sync_id: str) -> dict:
        """接取任务：旧项目状态机「等待中 → 讨伐中」。"""
        result = await self._request(
            "POST", f"/api/tasks/{task_id}/accept", acting_sync_id=acting_sync_id
        )
        return map_task(result)

    async def submit_task(
        self,
        task_id: str,
        *,
        acting_sync_id: str,
        description: Optional[str],
        attachment_url: Optional[str],
    ) -> dict:
        result = await self._request(
            "POST",
            f"/api/tasks/{task_id}/submit",
            acting_sync_id=acting_sync_id,
            json_body={
                "description": description or "",
                "attachmentUrl": attachment_url or "",
            },
        )
        return map_task(result)

    async def approve_task(self, task_id: str, *, acting_sync_id: str) -> dict:
        result = await self._request(
            "POST", f"/api/tasks/{task_id}/approve", acting_sync_id=acting_sync_id
        )
        return map_task(result)

    async def reject_task(
        self, task_id: str, *, acting_sync_id: str, reason: Optional[str]
    ) -> dict:
        result = await self._request(
            "POST",
            f"/api/tasks/{task_id}/reject",
            acting_sync_id=acting_sync_id,
            json_body={"reason": reason or ""},
        )
        return map_task(result)

    async def complete_task(self, task_id: str, *, acting_sync_id: str) -> dict:
        result = await self._request(
            "POST", f"/api/tasks/{task_id}/complete", acting_sync_id=acting_sync_id
        )
        return map_task(result)

    async def force_close_task(
        self, task_id: str, *, acting_sync_id: str, reason: Optional[str]
    ) -> dict:
        result = await self._request(
            "POST",
            f"/api/tasks/{task_id}/force-close",
            acting_sync_id=acting_sync_id,
            json_body={"reason": reason or ""},
        )
        return map_task(result)

    # -------------------------------------------------------------- 用户同步

    async def upsert_user(self, payload: dict) -> Optional[dict]:
        """推送用户到旧项目（系统级调用，无行为主体头）。"""
        return await self._request(
            "POST", "/api/internal/users/upsert", json_body=payload
        )

    async def fetch_user_changes(self, since: Optional[str]) -> list[dict]:
        """增量拉取旧项目自建 / 变更用户（按 updated_at 水位）。"""
        params = {"since": since} if since else None
        data = await self._request("GET", "/api/internal/users/changes", params=params)
        return data or []

    async def get_user_by_sync_id(self, sync_id: str) -> Optional[dict]:
        """按同步标识查询旧项目本地用户（身份桥接解析）。"""
        try:
            return await self._request(
                "GET", f"/api/internal/users/by-sync-id/{sync_id}"
            )
        except NotFoundException:
            return None


# 模块级单例：无状态，供依赖工厂与后台任务共用。
legacy_task_client = LegacyTaskClient()