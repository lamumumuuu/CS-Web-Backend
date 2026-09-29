"""业务配置（ER-55：TOTP / 验证码 / 密码 / SMTP / OAuth / LLM）。"""

import os
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class BusinessSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.environ.get("ENV_FILE", ".env"),
        case_sensitive=True,
        extra="ignore",
    )

    # TOTP 2FA 业务参数
    TOTP_ISSUER: str = "FZTBUCS"
    # TOTP 时间步长（秒）与允许的时钟偏移窗口（步）
    TOTP_STEP_SECONDS: int = Field(30, gt=0)
    TOTP_WINDOW_STEPS: int = Field(1, ge=0)
    # 2FA 预认证 token 有效期（分钟）：登录输入密码后、完成 TOTP 前的短期凭证
    TOTP_PRE_AUTH_TTL_MINUTES: int = Field(5, gt=0)

    # 邮箱验证码（注册/找回密码）
    VERIFICATION_CODE_TTL_MINUTES: int = Field(10, gt=0)

    # 密码历史复用检测（N 条；0 = 禁用）
    PASSWORD_HISTORY_LIMIT: int = Field(5, ge=0)
    # 管理员批准重置时使用的默认密码（运行时读取；未配置时审批接口拒绝执行）
    PASSWORD_RESET_DEFAULT: Optional[str] = None

    # SMTP 邮件（SMTP_HOST 为空时回退控制台输出，与前端行为一致）
    SMTP_HOST: Optional[str] = None
    SMTP_PORT: int = Field(587, gt=0)
    SMTP_SECURE: bool = False  # true = 隐式 TLS（SMTPS），false = STARTTLS
    SMTP_USER: Optional[str] = None
    SMTP_PASS: Optional[str] = None
    SMTP_FROM: str = "no-reply@fztbu.example"
    SMTP_TLS_SKIP_VERIFY: bool = False  # 仅本地开发；生产请配置可信 CA

    # GitHub OAuth（未配置 CLIENT_ID 时 OAuth 登录入口返回未启用）
    GITHUB_CLIENT_ID: Optional[str] = None
    GITHUB_CLIENT_SECRET: Optional[str] = None
    GITHUB_CALLBACK_URL: Optional[str] = (
        None  # 默认 {SITE_URL}/api/auth/oauth/github/callback
    )

    # LLM 学习助手配置（Auxilio Agent）
    # LLM_PROVIDER: openai（OpenAI 兼容协议）/ anthropic / none（禁用，回退规则推荐）
    LLM_PROVIDER: str = "none"
    LLM_API_KEY: Optional[str] = None  # 密钥只存 .env，绝不落库/日志/前端
    LLM_BASE_URL: Optional[str] = (
        None  # OpenAI 兼容自定义网关（Ollama、vLLM 等本地/第三方网关）
    )
    LLM_MODEL: str = "gpt-4o-mini"
    LLM_TIMEOUT: float = Field(60, gt=0)
    LLM_MAX_TOKENS: int = Field(1024, gt=0)
    # 单日每用户 LLM 调用预算（0 = 不限制），防成本失控。
    # 单位：千 tokens/日，默认 200 = 20 万 tokens；拦截逻辑在 auxilio_agent.run_chat（llm_usage_logs 按日累加）。  # noqa: E501
    LLM_DAILY_BUDGET: int = Field(200, ge=0)
    # 学习助手联网搜索工具开关（融合点 4）：false 时 web_search 工具返回未启用提示。
    # 搜索源为 DuckDuckGo 免费 HTML 接口（无需 API key），结果不可信，经 ER-19 包裹注入。
    WEB_SEARCH_ENABLED: bool = True

    # ---- 旧项目（FGS_CS_B）任务模块外接：BFF 代理与用户同步 ----
    # 任务数据唯一事实源为旧项目 tasks 表；官网后端作为 BFF 代理转发，不做双写。
    # LEGACY_API_BASE_URL 为空即视为外接链路禁用（代理端点返回 503，不影响官网其它模块）。
    LEGACY_API_BASE_URL: Optional[str] = None
    # 服务账号令牌签名密钥：必须与旧项目 JWT_SECRET 一致（旧项目据此识别内部调用）。
    # 该密钥仅出现在服务端之间，绝不外泄到浏览器；下次更新切换为 X-Internal-Token 共享密钥。
    LEGACY_JWT_SECRET: Optional[str] = None
    # 服务账号用户名：JWT 命中该值即被旧项目识别为内部调用。
    LEGACY_SERVICE_ACCOUNT: str = "service_account"
    # 行为主体头名：取值为官网用户的 sync_id，旧项目据此解析"以谁的身份"执行。
    LEGACY_ACTING_HEADER: str = "X-Acting-User-Id"
    # 来源标识头名：仅用于旧项目侧审计日志。
    LEGACY_SOURCE_HEADER: str = "X-Internal-Source"
    # 来源标识取值：标识本次调用来自官网后端。
    LEGACY_SOURCE_VALUE: str = "cs-web-backend"
    # 旧项目请求超时（秒）。
    LEGACY_TIMEOUT_SECONDS: float = Field(10.0, gt=0)
    # 服务账号令牌有效期（秒）：短 TTL，仅用于单次请求签名。
    LEGACY_SERVICE_TOKEN_TTL_SECONDS: int = Field(300, gt=0)
    # 用户同步对账间隔（秒）：默认 6 小时；<=0 表示禁用定时对账（注册实时推送仍生效）。
    LEGACY_SYNC_INTERVAL_SECONDS: int = Field(21600, ge=0)
    # 内部共享密钥：旧项目回推官网时校验来源（下次更新启用双向；为空时内部回推接口 fail-closed）。
    INTERNAL_TOKEN: str = ""

    @property
    def legacy_enabled(self) -> bool:
        """外接链路是否可用：基地址与服务账号密钥均配置时才启用。"""
        return bool(self.LEGACY_API_BASE_URL and self.LEGACY_JWT_SECRET)
