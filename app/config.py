from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Python Practice PoC"
    app_env: Literal["development", "test", "production"] = "development"
    auth_mode: Literal["demo", "session"] = "demo"
    demo_login_name: str = "demo"
    auth_session_secret: str | None = None
    auth_session_ttl_seconds: int = Field(default=28_800, ge=300, le=604_800)
    auth_cookie_secure: bool = False
    auth_cookie_name: str = "python_practice_session"
    auth_csrf_cookie_name: str = "python_practice_csrf"
    auth_login_max_attempts: int = Field(default=5, ge=1, le=100)
    auth_login_window_seconds: int = Field(default=300, ge=10, le=3_600)
    trusted_hosts: str = "localhost,127.0.0.1,testserver"
    worker_heartbeat_max_age_seconds: int = Field(default=30, ge=5, le=300)
    max_source_code_length: int = Field(default=20_000, ge=100, le=200_000)
    max_pending_submissions_per_student: int = Field(default=5, ge=1, le=50)
    history_max_items: int = Field(default=100, ge=1, le=500)
    database_url: str = Field(
        default="postgresql+psycopg://python_poc:python_poc@localhost:5432/python_poc"
    )
    db_pool_size: int = Field(default=10, ge=1, le=100)
    db_max_overflow: int = Field(default=20, ge=0, le=100)
    db_pool_timeout_seconds: float = Field(default=30.0, gt=0)
    db_pool_recycle_seconds: float = Field(default=1_800, gt=0)

    judge0_base_url: str = "http://localhost:2358"
    judge0_language_id: int | None = None
    judge0_auth_token: str | None = None
    judge0_auth_user: str | None = None
    judge0_poll_interval_seconds: float = Field(default=0.5, gt=0)
    judge0_poll_timeout_seconds: float = Field(default=15.0, gt=0)
    judge0_cpu_time_limit_seconds: float = Field(default=5.0, gt=0)
    judge0_wall_time_limit_seconds: float = Field(default=10.0, gt=0)
    judge0_memory_limit_kb: int = Field(default=256_000, gt=0)
    judge0_max_file_size_kb: int = Field(default=1_024, gt=0)
    grading_stale_run_timeout_seconds: int = Field(default=180, ge=30, le=3_600)
    worker_concurrency: int = Field(default=4, ge=1, le=16)

    # Cấu hình Hạn mức & Gợi ý sư phạm KM/KH (Chương 2 - Mục 2.2, 2.4, 2.5)
    # Ràng buộc le=3 đồng bộ với CheckConstraint(used_count <= 3) của bảng help_quota.
    hint_max_quota: int = Field(default=3, ge=1, le=3)
    hint_cooldown_seconds: int = Field(default=120, ge=0)
    hint_request_expire_seconds: int = Field(default=60, gt=0)
    hint_execution_mode: Literal["sync", "worker"] = "sync"
    hint_worker_claim_timeout_seconds: int = Field(default=90, ge=30, le=3_600)
    hint_worker_max_attempts: int = Field(default=2, ge=1, le=5)
    hint_max_evidence: int = Field(default=2, ge=1, le=10)
    hint_max_retries: int = Field(default=2, ge=0, le=5)
    hint_allow_hidden_only: bool = False
    hint_max_failed_attempts_per_window: int = Field(default=3, ge=1, le=20)
    hint_failed_window_seconds: int = Field(default=300, ge=10, le=3_600)

    # Cấu hình Chiến lược LLM lai (Adapter Pattern: APIDemoAdapter & ColabQwenAdapter)
    llm_mode: Literal["api_demo", "colab_qwen", "fake", "disabled"] = "api_demo"
    llm_api_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str | None = None
    llm_model_id: str = "gpt-4o-mini"
    llm_temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    llm_timeout_seconds: float = Field(default=15.0, gt=0)
    llm_max_tokens: int = Field(default=400, ge=50, le=2000)
    colab_qwen_model_id: str = "Qwen/Qwen2.5-Coder-7B-Instruct"
    colab_qwen_endpoint_url: str | None = None
    colab_qwen_api_key: str | None = None
    guardrail_token_overlap_threshold: float = Field(default=0.75, gt=0.0, le=1.0)

    @field_validator("judge0_language_id", mode="before")
    @classmethod
    def empty_language_id_is_none(cls, value: object) -> object:
        if value == "":
            return None
        return value

    @model_validator(mode="after")
    def validate_production_security(self) -> "Settings":
        if self.app_env != "production":
            return self
        if self.auth_mode != "session":
            raise ValueError("production_requires_auth_mode_session")
        if not self.auth_session_secret or len(self.auth_session_secret) < 32:
            raise ValueError("production_requires_auth_session_secret_min_32_chars")
        if self.auth_session_secret.startswith("REPLACE_WITH"):
            raise ValueError("production_auth_session_secret_is_placeholder")
        if not self.auth_cookie_secure:
            raise ValueError("production_requires_secure_auth_cookie")
        if self.llm_mode == "fake":
            raise ValueError("production_forbids_fake_llm")
        if self.llm_mode == "api_demo" and (
            not self.llm_api_key or self.llm_api_key.startswith("REPLACE_WITH")
        ):
            raise ValueError("production_requires_real_llm_api_key")
        if self.llm_mode == "api_demo" and not self.llm_api_base_url.startswith(
            "https://"
        ):
            raise ValueError("production_requires_https_llm_url")
        if self.llm_mode == "colab_qwen" and (
            not self.colab_qwen_endpoint_url
            or self.colab_qwen_endpoint_url.startswith("REPLACE_WITH")
            or not self.colab_qwen_endpoint_url.startswith("https://")
            or (
                self.colab_qwen_api_key
                and self.colab_qwen_api_key.startswith("REPLACE_WITH")
            )
        ):
            raise ValueError("production_requires_https_colab_qwen_endpoint")
        if self.judge0_language_id is None:
            raise ValueError("production_requires_judge0_language_id")
        if (
            self.judge0_auth_token is not None
            and (
                not self.judge0_auth_token.strip()
                or self.judge0_auth_token.startswith("REPLACE_WITH")
            )
        ):
            raise ValueError("production_judge0_auth_token_is_placeholder")
        if "ce.judge0.com" in self.judge0_base_url or "localhost" in self.judge0_base_url:
            raise ValueError("production_requires_private_judge0")
        if not self.judge0_base_url.startswith("https://"):
            raise ValueError("production_requires_https_judge0_url")
        if (
            self.database_url
            == "postgresql+psycopg://python_poc:python_poc@localhost:5432/python_poc"
            or "URL_ENCODED_PASSWORD" in self.database_url
            or "REPLACE_WITH" in self.database_url
        ):
            raise ValueError("production_requires_nondefault_database_url")
        if not self.trusted_host_list or "*" in self.trusted_host_list:
            raise ValueError("production_forbids_wildcard_trusted_hosts")
        if "testserver" in self.trusted_host_list:
            raise ValueError("production_requires_explicit_trusted_hosts")
        if self.hint_execution_mode != "worker":
            raise ValueError("production_requires_hint_execution_mode_worker")
        if self.hint_request_expire_seconds <= self.hint_worker_claim_timeout_seconds:
            raise ValueError("production_hint_expiry_must_exceed_worker_claim_timeout")
        minimum_claim_seconds = (
            self.llm_timeout_seconds * (self.hint_max_retries + 1) + 5
        )
        if self.hint_worker_claim_timeout_seconds < minimum_claim_seconds:
            raise ValueError("hint_worker_claim_timeout_too_short_for_llm_retries")
        return self

    @property
    def trusted_host_list(self) -> list[str]:
        return [item.strip() for item in self.trusted_hosts.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
