# mypy: ignore-errors
"""任务 API：官网 BFF 任务代理端点。

任务数据的唯一事实源是旧项目（FGS_CS_B）``tasks`` 表。本模块不落库、不读写官网
本地 ``tasks`` 表，全部端点转发到 ``legacy_task_client``；「旧项目中文状态 → 官网
英文状态」与字段名映射均在代理层单点完成，对外契约与官网前端 BFF 保持一致。

路径契约（单数 ``/tools/task``，与前端 BFF 的 ``proxyBackend`` 一致）：
- ``GET  /task``                          任务大厅列表（内部通道放开峰过滤）
- ``GET  /task/claims``                   我的任务（我接取的）
- ``POST /task/claims/{task_id}/submit``  提交成果
- ``GET  /task/{task_id}``                任务详情
- ``POST /task/{task_id}/claim``          接取任务（旧项目 accept 语义）
- ``/admin/task*``                        管理端：待审核列表 / 建单 / 审核 / 验收 / 强结 / 删除
"""

from __future__ import annotations

from math import ceil
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query

from app.dependencies import get_current_active_user, get_optional_current_user
from app.dependencies_services import get_legacy_task_client, get_legacy_user_sync
from app.middleware.rbac import require_permission
from app.models.user import User
from app.schemas.tools import TaskActionIn, TaskInput, TaskSubmitIn
from app.services.legacy_task_client import LegacyTaskClient
from app.services.legacy_user_sync import LegacyUserSync

router = APIRouter()


async def get_acting_sync_id(
    current_user: User = Depends(get_current_active_user),
    sync: LegacyUserSync = Depends(get_legacy_user_sync),
) -> str:
    """行为主体标识：取当前用户 ``sync_id``，缺失则补发并落库后再调用旧项目。"""
    return await sync.ensure_sync_id(current_user)


def _paginate(items: list[dict], page: int, page_size: int) -> dict:
    """旧项目列表不分页，按官网对外契约在代理层内存切片。"""
    total = len(items)
    start = (page - 1) * page_size
    return {
        "items": items[start : start + page_size],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, ceil(total / page_size)),
    }


def _claim_from_task(task: dict, sync_id: str) -> dict:
    """官网无 claim_id 概念：以 task_id 为键合成 claim 契约供前端展示。"""
    return {
        "id": task.get("id"),
        "task_id": task.get("id"),
        "user_id": sync_id,
        "status": task.get("status"),
        "status_label": task.get("status_label"),
        "created_at": task.get("updated_at") or task.get("created_at"),
        "task": task,
    }


# ------------------------------------------------------------------ 大厅 / 详情


@router.get("/task")
async def list_tasks(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    difficulty: Optional[str] = None,
    status: Optional[str] = None,
    keyword: Optional[str] = None,
    current_user: Optional[User] = Depends(get_optional_current_user),
    client: LegacyTaskClient = Depends(get_legacy_task_client),
) -> Any:
    """任务大厅列表：内部通道调用旧项目，放开峰过滤，返回全量已发布任务。"""
    tasks = await client.list_tasks(
        acting_sync_id=current_user.sync_id if current_user else None,
        difficulty=difficulty,
        status=status,
        keyword=keyword,
    )
    return _paginate(tasks, page, page_size)


@router.get("/task/claims")
async def my_tasks(
    client: LegacyTaskClient = Depends(get_legacy_task_client),
    sync_id: str = Depends(get_acting_sync_id),
) -> Any:
    """我的任务：按内部身份返回该用户接取的任务。"""
    tasks = await client.my_accepted(acting_sync_id=sync_id)
    return {"items": tasks, "total": len(tasks)}


@router.post("/task/claims/{task_id}/submit")
async def submit_task(
    task_id: str,
    body: TaskSubmitIn,
    client: LegacyTaskClient = Depends(get_legacy_task_client),
    sync_id: str = Depends(get_acting_sync_id),
) -> Any:
    """提交成果：描述 + 附件链接直接对应旧项目 submit 语义。"""
    task = await client.submit_task(
        task_id,
        acting_sync_id=sync_id,
        description=body.effective_description,
        attachment_url=body.effective_attachment,
    )
    return _claim_from_task(task, sync_id)


@router.get("/task/{task_id}")
async def get_task(
    task_id: str,
    current_user: Optional[User] = Depends(get_optional_current_user),
    client: LegacyTaskClient = Depends(get_legacy_task_client),
) -> Any:
    return await client.get_task(
        task_id, acting_sync_id=current_user.sync_id if current_user else None
    )


@router.post("/task/{task_id}/claim")
async def claim_task(
    task_id: str,
    client: LegacyTaskClient = Depends(get_legacy_task_client),
    sync_id: str = Depends(get_acting_sync_id),
) -> Any:
    """接取任务：旧项目状态机「等待中 → 讨伐中」。"""
    task = await client.accept_task(task_id, acting_sync_id=sync_id)
    return _claim_from_task(task, sync_id)


# ------------------------------------------------------------------ 管理端


@router.get("/admin/task")
async def admin_list_tasks(
    client: LegacyTaskClient = Depends(get_legacy_task_client),
    current_user: User = Depends(require_permission("task", "read")),
    sync_id: str = Depends(get_acting_sync_id),
) -> Any:
    """待审核任务列表（旧项目「审核中」）。"""
    tasks = await client.pending_tasks(acting_sync_id=sync_id)
    return {"items": tasks, "total": len(tasks)}


@router.post("/admin/task", status_code=201)
async def create_task(
    body: TaskInput,
    client: LegacyTaskClient = Depends(get_legacy_task_client),
    current_user: User = Depends(require_permission("task", "create")),
    sync_id: str = Depends(get_acting_sync_id),
) -> Any:
    """建单：旧项目落库后进入「审核中」。"""
    return await client.create_task(acting_sync_id=sync_id, data=body.model_dump())


@router.put("/admin/task/{task_id}")
async def update_task(
    task_id: str,
    body: TaskInput,
    client: LegacyTaskClient = Depends(get_legacy_task_client),
    current_user: User = Depends(require_permission("task", "update")),
    sync_id: str = Depends(get_acting_sync_id),
) -> Any:
    return await client.update_task(task_id, acting_sync_id=sync_id, data=body.model_dump())


@router.post("/admin/task/{task_id}/approve")
async def approve_task(
    task_id: str,
    client: LegacyTaskClient = Depends(get_legacy_task_client),
    current_user: User = Depends(require_permission("task", "review")),
    sync_id: str = Depends(get_acting_sync_id),
) -> Any:
    """审核通过：旧项目「审核中 → 等待中」。"""
    return await client.approve_task(task_id, acting_sync_id=sync_id)


@router.post("/admin/task/{task_id}/reject")
async def reject_task(
    task_id: str,
    body: Optional[TaskActionIn] = None,
    client: LegacyTaskClient = Depends(get_legacy_task_client),
    current_user: User = Depends(require_permission("task", "review")),
    sync_id: str = Depends(get_acting_sync_id),
) -> Any:
    """审核驳回：旧项目「审核中 → 已驳回」，原因透传。"""
    return await client.reject_task(
        task_id, acting_sync_id=sync_id, reason=body.reason if body else None
    )


@router.post("/admin/task/{task_id}/complete")
async def complete_task(
    task_id: str,
    client: LegacyTaskClient = Depends(get_legacy_task_client),
    current_user: User = Depends(require_permission("task", "review")),
    sync_id: str = Depends(get_acting_sync_id),
) -> Any:
    """验收通过：旧项目「已提交 → 已完成」。"""
    return await client.complete_task(task_id, acting_sync_id=sync_id)


@router.post("/admin/task/{task_id}/force-close")
async def force_close_task(
    task_id: str,
    body: Optional[TaskActionIn] = None,
    client: LegacyTaskClient = Depends(get_legacy_task_client),
    current_user: User = Depends(require_permission("task", "close")),
    sync_id: str = Depends(get_acting_sync_id),
) -> Any:
    """强制结项：旧项目「→ 已强制结项」，原因透传。"""
    return await client.force_close_task(
        task_id, acting_sync_id=sync_id, reason=body.reason if body else None
    )


@router.delete("/admin/task/{task_id}")
async def delete_task(
    task_id: str,
    client: LegacyTaskClient = Depends(get_legacy_task_client),
    current_user: User = Depends(require_permission("task", "delete")),
    sync_id: str = Depends(get_acting_sync_id),
) -> Any:
    await client.delete_task(task_id, acting_sync_id=sync_id)
    return {"ok": True}