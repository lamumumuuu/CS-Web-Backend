"""internal 业务域路由包：仅服务端之间调用的内部接口。

当前承载官网后端接收旧项目回推用户的接口（``/internal/users/upsert``）。
不对外暴露、不经浏览器；来源校验由共享密钥 ``INTERNAL_TOKEN`` 承担。
"""

from app.api.v1.internal import users

__all__ = ["users"]