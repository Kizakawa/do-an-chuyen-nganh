from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import logging
import math
import re
import secrets
from typing import Any, Callable, Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.hint_context import HintContextV1, HintEligibilityError, build_hint_context
from app.hint_context.schemas import PublicEvidence
from app.hints.adapters import LLMServiceInterface, build_llm_adapter
from app.hints.guardrail import check_output_guardrail
from app.hints.prompts import PROMPT_VERSION, build_versioned_prompt
from app.models import HelpQuota, HintRequest, Problem


logger = logging.getLogger(__name__)

FUNC_NAME_RE = re.compile(r"^([a-zA-Z_]\w*)\s*\(")
DB_HARD_MAX_QUOTA = 3


class HintRequestRejectedError(ValueError):
    def __init__(
        self,
        code: str,
        *,
        status_code: int = 409,
        retry_after_seconds: int | None = None,
        remaining_count: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.remaining_count = remaining_count


@dataclass(frozen=True)
class CoreHintResult:
    valid_hint: str | None
    prompt_version: str
    model_id: str
    provider: str
    generation_log: list[dict[str, Any]]
    any_leaked: bool
    error_code: str | None


@dataclass(frozen=True)
class HintWorkItem:
    request_id: int
    claim_token: str
    student_id: int
    problem_id: int
    level: Literal["KM", "KH"]
    context: HintContextV1
    reference_code: str | None


@dataclass(frozen=True)
class QuotaStatusView:
    problem_id: int
    used_count: int
    max_quota: int
    remaining_count: int
    next_level: Literal["KM", "KH"] | None
    cooldown_remaining_seconds: int
    next_allowed_at: datetime | None
    has_processing_request: bool


def _extract_function_name(signature: str) -> str | None:
    match = FUNC_NAME_RE.match(signature.strip())
    return match.group(1) if match else None


def generate_and_validate_hint_core(
    context: HintContextV1,
    level: Literal["KM", "KH"],
    adapter: LLMServiceInterface,
    *,
    max_retries: int = 2,
    reference_code: str | None = None,
    token_overlap_threshold: float = 0.75,
    temperature: float = 0.2,
    timeout_seconds: float = 15.0,
    max_tokens: int = 400,
) -> CoreHintResult:
    """Phần A Thuật toán 2.2 (Mục 2.4.4 Chương 2): Đóng gói Prompt, gọi LLM và lọc Guardrail."""
    function_name = _extract_function_name(context.problem.function_signature)
    generation_log: list[dict[str, Any]] = []
    valid_hint: str | None = None
    any_leaked = False
    error_code: str | None = None

    for attempt in range(max_retries + 1):
        built_prompt = build_versioned_prompt(context, level, attempt=attempt)
        gen = adapter.generate(
            system_prompt=built_prompt.system_prompt,
            user_prompt=built_prompt.user_prompt,
            temperature=temperature,
            timeout_seconds=timeout_seconds,
            max_tokens=max_tokens,
        )
        if gen.error is not None or gen.raw_text is None:
            error_code = gen.error or "llm_empty_response"
            generation_log.append(
                {
                    "attempt": attempt,
                    "provider": gen.provider,
                    "model_id": gen.model_id,
                    "latency_ms": gen.latency_ms,
                    "is_leaked": False,
                    "leak_reasons": [],
                    "error": error_code,
                }
            )
            break

        check = check_output_guardrail(
            gen.raw_text,
            reference_code=reference_code,
            function_name=function_name,
            token_overlap_threshold=token_overlap_threshold,
        )
        generation_log.append(
            {
                "attempt": attempt,
                "provider": gen.provider,
                "model_id": gen.model_id,
                "latency_ms": gen.latency_ms,
                "raw_text": gen.raw_text,
                "is_leaked": check.is_leaked,
                "leak_reasons": check.reasons,
                "token_overlap": check.token_overlap,
                "prompt_tokens": gen.prompt_tokens,
                "completion_tokens": gen.completion_tokens,
                "error": None,
            }
        )
        if not check.is_leaked:
            valid_hint = gen.raw_text.strip()
            error_code = None
            break

        any_leaked = True
        error_code = "guardrail_leaked"

    return CoreHintResult(
        valid_hint=valid_hint,
        prompt_version=PROMPT_VERSION,
        model_id=adapter.model_id,
        provider=adapter.provider_name,
        generation_log=generation_log,
        any_leaked=any_leaked,
        error_code=error_code,
    )


class HintOrchestrationService:
    """Phần B Thuật toán 2.2 (Mục 2.4.4 & 2.5.6 Chương 2): Điều phối yêu cầu gợi ý trực tuyến (UC05)."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        adapter: LLMServiceInterface | None = None,
        settings: Settings | None = None,
        now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings or get_settings()
        self.adapter = adapter or build_llm_adapter(self.settings)
        self.now_fn = now_fn

    @property
    def effective_max_quota(self) -> int:
        return min(self.settings.hint_max_quota, DB_HARD_MAX_QUOTA)

    def _expire_stale_requests(
        self,
        db: Session,
        *,
        student_id: int,
        problem_id: int,
        now: datetime,
    ) -> list[HintRequest]:
        stale_requests = list(
            db.scalars(
                select(HintRequest)
                .where(
                    HintRequest.student_id == student_id,
                    HintRequest.problem_id == problem_id,
                    HintRequest.status == "processing",
                    HintRequest.expires_at <= now,
                )
                .with_for_update()
            )
        )
        for req in stale_requests:
            req.status = "failed"
            req.error_code = "request_expired"
            req.completed_at = now
        return stale_requests

    @staticmethod
    def _expire_request_if_needed(
        req: HintRequest,
        *,
        now: datetime,
    ) -> bool:
        if req.status != "processing" or req.expires_at > now:
            return False
        req.status = "failed"
        req.error_code = "request_expired"
        req.completed_at = now
        return True

    def get_quota_status(
        self,
        db: Session,
        *,
        student_id: int,
        problem_id: int,
    ) -> QuotaStatusView:
        now = self.now_fn()
        quota = db.get(HelpQuota, (student_id, problem_id))
        used_count = quota.used_count if quota is not None else 0
        next_allowed_at = quota.next_allowed_at if quota is not None else None
        max_quota = self.effective_max_quota
        remaining = max(0, max_quota - used_count)

        cooldown_remaining = 0
        if next_allowed_at is not None and now < next_allowed_at:
            cooldown_remaining = max(
                1, math.ceil((next_allowed_at - now).total_seconds())
            )

        active_processing = db.scalar(
            select(HintRequest.id).where(
                HintRequest.student_id == student_id,
                HintRequest.problem_id == problem_id,
                HintRequest.status == "processing",
                HintRequest.expires_at > now,
            )
        )
        next_level: Literal["KM", "KH"] | None = None
        if remaining > 0:
            next_level = "KM" if used_count == 0 else "KH"

        return QuotaStatusView(
            problem_id=problem_id,
            used_count=used_count,
            max_quota=max_quota,
            remaining_count=remaining,
            next_level=next_level,
            cooldown_remaining_seconds=cooldown_remaining,
            next_allowed_at=next_allowed_at,
            has_processing_request=active_processing is not None,
        )

    def enqueue_hint(
        self,
        *,
        student_id: int,
        submission_id: int,
        grading_run_id: int,
        request_key: str,
    ) -> tuple[HintRequest, QuotaStatusView]:
        cleaned_key = request_key.strip()
        if not cleaned_key:
            raise HintRequestRejectedError("invalid_request_key", status_code=422)

        now = self.now_fn()

        # Bước 1: Kiểm tra idempotency key trong giao dịch ngắn. Nếu request
        # cũ đã hết hạn, phải lưu trạng thái failed xuống DB trước khi trả về.
        with self.session_factory.begin() as db:
            existing = db.execute(
                select(HintRequest)
                .where(
                    HintRequest.student_id == student_id,
                    HintRequest.request_key == cleaned_key,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if existing is not None:
                self._expire_request_if_needed(existing, now=now)
                db.flush()
                quota_view = self.get_quota_status(
                    db, student_id=student_id, problem_id=existing.problem_id
                )
                db.expunge(existing)
                return existing, quota_view

        # Dựng context ngoài giao dịch khóa quota/LLM.
        with self.session_factory() as db:
            try:
                context = build_hint_context(
                    db,
                    student_id=student_id,
                    submission_id=submission_id,
                    grading_run_id=grading_run_id,
                    max_evidence=self.settings.hint_max_evidence,
                )
            except HintEligibilityError as exc:
                status_map = {
                    "not_owner": 404,
                    "grading_run_not_found": 404,
                    "grading_pending": 409,
                    "grading_technical_error": 409,
                    "all_passed": 409,
                }
                raise HintRequestRejectedError(
                    exc.code, status_code=status_map.get(exc.code, 409)
                ) from exc

            has_public_evidence = any(
                isinstance(item, PublicEvidence) for item in context.evidence
            )
            if not has_public_evidence and not self.settings.hint_allow_hidden_only:
                raise HintRequestRejectedError(
                    "no_public_evidence_available", status_code=409
                )

        problem_id = context.problem_id
        max_quota = self.effective_max_quota

        # Bước 2: Giao dịch ngắn khóa HelpQuota (SELECT FOR UPDATE) và tạo HintRequest(processing)
        now = self.now_fn()
        try:
            with self.session_factory.begin() as db:
                existing = db.execute(
                    select(HintRequest)
                    .where(
                        HintRequest.student_id == student_id,
                        HintRequest.request_key == cleaned_key,
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                if existing is not None:
                    self._expire_request_if_needed(existing, now=now)
                    db.flush()
                    quota_view = self.get_quota_status(
                        db, student_id=student_id, problem_id=existing.problem_id
                    )
                    db.expunge(existing)
                    return existing, quota_view

                upsert_stmt = (
                    insert(HelpQuota)
                    .values(
                        student_id=student_id,
                        problem_id=problem_id,
                        used_count=0,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[HelpQuota.student_id, HelpQuota.problem_id]
                    )
                )
                db.execute(upsert_stmt)

                quota = db.execute(
                    select(HelpQuota)
                    .where(
                        HelpQuota.student_id == student_id,
                        HelpQuota.problem_id == problem_id,
                    )
                    .with_for_update()
                ).scalar_one()

                self._expire_stale_requests(
                    db, student_id=student_id, problem_id=problem_id, now=now
                )
                db.flush()

                in_flight = db.scalar(
                    select(HintRequest.id).where(
                        HintRequest.student_id == student_id,
                        HintRequest.problem_id == problem_id,
                        HintRequest.status == "processing",
                    )
                )
                remaining_before = max(0, max_quota - quota.used_count)
                if in_flight is not None:
                    raise HintRequestRejectedError(
                        "request_already_processing",
                        status_code=409,
                        remaining_count=remaining_before,
                    )
                if quota.used_count >= max_quota:
                    raise HintRequestRejectedError(
                        "quota_exhausted",
                        status_code=429,
                        remaining_count=0,
                    )
                if (
                    quota.next_allowed_at is not None
                    and now < quota.next_allowed_at
                ):
                    retry_after = max(
                        1,
                        math.ceil((quota.next_allowed_at - now).total_seconds()),
                    )
                    raise HintRequestRejectedError(
                        "cooldown_active",
                        status_code=429,
                        retry_after_seconds=retry_after,
                        remaining_count=remaining_before,
                    )

                failure_window_start = now - timedelta(
                    seconds=self.settings.hint_failed_window_seconds
                )
                recent_failed_times = list(
                    db.scalars(
                        select(HintRequest.completed_at)
                        .where(
                            HintRequest.student_id == student_id,
                            HintRequest.status == "failed",
                            HintRequest.completed_at.is_not(None),
                            HintRequest.completed_at > failure_window_start,
                        )
                        .order_by(HintRequest.completed_at.asc())
                    )
                )
                if (
                    len(recent_failed_times)
                    >= self.settings.hint_max_failed_attempts_per_window
                ):
                    oldest_failure = recent_failed_times[0]
                    assert oldest_failure is not None
                    retry_after = max(
                        1,
                        math.ceil(
                            (
                                oldest_failure
                                + timedelta(
                                    seconds=self.settings.hint_failed_window_seconds
                                )
                                - now
                            ).total_seconds()
                        ),
                    )
                    raise HintRequestRejectedError(
                        "hint_failure_rate_limited",
                        status_code=429,
                        retry_after_seconds=retry_after,
                        remaining_count=remaining_before,
                    )

                level: Literal["KM", "KH"] = "KM" if quota.used_count == 0 else "KH"
                expires_at = now + timedelta(
                    seconds=self.settings.hint_request_expire_seconds
                )
                req = HintRequest(
                    student_id=student_id,
                    problem_id=problem_id,
                    submission_id=submission_id,
                    grading_run_id=grading_run_id,
                    request_key=cleaned_key,
                    level=level,
                    status="processing",
                    hint_text=None,
                    prompt_version=PROMPT_VERSION,
                    model_id=self.adapter.model_id,
                    evidence_snapshot=context.model_dump(),
                    generation_log=[],
                    is_leaked=False,
                    error_code=None,
                    created_at=now,
                    expires_at=expires_at,
                )
                db.add(req)
                db.flush()
                request_id = req.id
        except IntegrityError as exc:
            with self.session_factory.begin() as db:
                existing = db.execute(
                    select(HintRequest)
                    .where(
                        HintRequest.student_id == student_id,
                        HintRequest.request_key == cleaned_key,
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                if existing is not None:
                    self._expire_request_if_needed(existing, now=now)
                    db.flush()
                    quota_view = self.get_quota_status(
                        db, student_id=student_id, problem_id=existing.problem_id
                    )
                    db.expunge(existing)
                    return existing, quota_view
            raise HintRequestRejectedError(
                "request_already_processing", status_code=409
            ) from exc

        logger.info(
            "hint_request_queued hint_request_id=%s student_id=%s problem_id=%s "
            "submission_id=%s grading_run_id=%s level=%s provider=%s model=%s",
            request_id,
            student_id,
            problem_id,
            submission_id,
            grading_run_id,
            level,
            self.adapter.provider_name,
            self.adapter.model_id,
        )
        with self.session_factory() as db:
            queued = db.get(HintRequest, request_id)
            if queued is None:
                raise RuntimeError(f"hint_request_missing_after_enqueue:{request_id}")
            quota_view = self.get_quota_status(
                db, student_id=student_id, problem_id=problem_id
            )
            db.expunge(queued)
            return queued, quota_view

    def _sweep_unclaimable_requests(
        self, db: Session, *, now: datetime, stale_before: datetime
    ) -> None:
        expired = list(
            db.scalars(
                select(HintRequest)
                .where(
                    HintRequest.status == "processing",
                    HintRequest.expires_at <= now,
                )
                .with_for_update(skip_locked=True)
            )
        )
        for req in expired:
            req.status = "failed"
            req.error_code = "request_expired"
            req.claim_token = None
            req.completed_at = now

        exhausted = list(
            db.scalars(
                select(HintRequest)
                .where(
                    HintRequest.status == "processing",
                    HintRequest.expires_at > now,
                    HintRequest.attempt_count >= self.settings.hint_worker_max_attempts,
                    HintRequest.claimed_at.is_not(None),
                    HintRequest.claimed_at <= stale_before,
                )
                .with_for_update(skip_locked=True)
            )
        )
        for req in exhausted:
            req.status = "failed"
            req.error_code = "worker_max_attempts_exceeded"
            req.claim_token = None
            req.completed_at = now

    def _claim_work(self, request_id: int | None = None) -> HintWorkItem | None:
        now = self.now_fn()
        stale_before = now - timedelta(
            seconds=self.settings.hint_worker_claim_timeout_seconds
        )
        with self.session_factory.begin() as db:
            self._sweep_unclaimable_requests(db, now=now, stale_before=stale_before)
            db.flush()
            query = (
                select(HintRequest)
                .where(
                    HintRequest.status == "processing",
                    HintRequest.expires_at > now,
                    HintRequest.attempt_count < self.settings.hint_worker_max_attempts,
                    (HintRequest.claimed_at.is_(None))
                    | (HintRequest.claimed_at <= stale_before),
                )
                .order_by(HintRequest.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if request_id is not None:
                query = query.where(HintRequest.id == request_id)
            req = db.execute(query).scalar_one_or_none()
            if req is None:
                return None
            claim_token = secrets.token_urlsafe(24)
            req.claimed_at = now
            req.claim_token = claim_token
            req.attempt_count += 1
            reference_code = db.scalar(
                select(Problem.reference_code).where(Problem.id == req.problem_id)
            )
            context = HintContextV1.model_validate(req.evidence_snapshot)
            return HintWorkItem(
                request_id=req.id,
                claim_token=claim_token,
                student_id=req.student_id,
                problem_id=req.problem_id,
                level=req.level,  # type: ignore[arg-type]
                context=context,
                reference_code=reference_code,
            )

    def _finish_work(
        self, work: HintWorkItem, core_result: CoreHintResult
    ) -> tuple[HintRequest, QuotaStatusView] | None:
        completed_now = self.now_fn()
        max_quota = self.effective_max_quota
        with self.session_factory.begin() as db:
            quota = db.execute(
                select(HelpQuota)
                .where(
                    HelpQuota.student_id == work.student_id,
                    HelpQuota.problem_id == work.problem_id,
                )
                .with_for_update()
            ).scalar_one()
            req = db.execute(
                select(HintRequest)
                .where(HintRequest.id == work.request_id)
                .with_for_update()
            ).scalar_one()
            if req.status != "processing" or req.claim_token != work.claim_token:
                return None

            used_before = quota.used_count
            req.generation_log = core_result.generation_log
            req.prompt_version = core_result.prompt_version
            req.model_id = core_result.model_id
            req.claim_token = None

            if completed_now > req.expires_at:
                req.status = "failed"
                req.error_code = "request_expired"
                req.is_leaked = core_result.any_leaked
                req.completed_at = completed_now
            elif core_result.valid_hint is not None and quota.used_count < max_quota:
                req.status = "succeeded"
                req.hint_text = core_result.valid_hint
                req.is_leaked = False
                req.error_code = None
                req.completed_at = completed_now
                quota.used_count += 1
                quota.next_allowed_at = completed_now + timedelta(
                    seconds=self.settings.hint_cooldown_seconds
                )
                quota.updated_at = completed_now
            else:
                req.status = "failed"
                req.hint_text = None
                req.is_leaked = core_result.any_leaked
                req.error_code = core_result.error_code or "llm_generation_failed"
                req.completed_at = completed_now

            db.flush()
            used_after = quota.used_count
            final_status = req.status
            final_error = req.error_code
            quota_view = self.get_quota_status(
                db, student_id=work.student_id, problem_id=work.problem_id
            )
            db.expunge(req)

        logger.info(
            "hint_request_finished hint_request_id=%s status=%s level=%s "
            "provider=%s model=%s is_leaked=%s used_before=%s used_after=%s error_code=%s",
            work.request_id,
            final_status,
            work.level,
            core_result.provider,
            core_result.model_id,
            core_result.any_leaked,
            used_before,
            used_after,
            final_error,
        )
        return req, quota_view

    def _fail_work(self, work: HintWorkItem, error_code: str) -> None:
        completed_now = self.now_fn()
        with self.session_factory.begin() as db:
            req = db.execute(
                select(HintRequest)
                .where(HintRequest.id == work.request_id)
                .with_for_update()
            ).scalar_one_or_none()
            if (
                req is not None
                and req.status == "processing"
                and req.claim_token == work.claim_token
            ):
                req.status = "failed"
                req.error_code = error_code
                req.claim_token = None
                req.completed_at = completed_now

    def process_request(
        self, request_id: int
    ) -> tuple[HintRequest, QuotaStatusView] | None:
        work = self._claim_work(request_id)
        if work is None:
            with self.session_factory() as db:
                req = db.get(HintRequest, request_id)
                if req is None:
                    return None
                quota_view = self.get_quota_status(
                    db, student_id=req.student_id, problem_id=req.problem_id
                )
                db.expunge(req)
                return req, quota_view
        try:
            core_result = generate_and_validate_hint_core(
                work.context,
                work.level,
                self.adapter,
                max_retries=self.settings.hint_max_retries,
                reference_code=work.reference_code,
                token_overlap_threshold=self.settings.guardrail_token_overlap_threshold,
                temperature=self.settings.llm_temperature,
                timeout_seconds=self.settings.llm_timeout_seconds,
                max_tokens=self.settings.llm_max_tokens,
            )
        except Exception:
            logger.exception("hint_worker_unhandled_error request_id=%s", request_id)
            self._fail_work(work, "llm_worker_error")
            return self.process_request(request_id)
        except BaseException:
            self._fail_work(work, "request_aborted")
            raise
        return self._finish_work(work, core_result)

    def process_next(self) -> bool:
        work = self._claim_work()
        if work is None:
            return False
        try:
            core_result = generate_and_validate_hint_core(
                work.context,
                work.level,
                self.adapter,
                max_retries=self.settings.hint_max_retries,
                reference_code=work.reference_code,
                token_overlap_threshold=self.settings.guardrail_token_overlap_threshold,
                temperature=self.settings.llm_temperature,
                timeout_seconds=self.settings.llm_timeout_seconds,
                max_tokens=self.settings.llm_max_tokens,
            )
            self._finish_work(work, core_result)
        except Exception:
            logger.exception("hint_worker_unhandled_error request_id=%s", work.request_id)
            self._fail_work(work, "llm_worker_error")
        except BaseException:
            self._fail_work(work, "request_aborted")
            raise
        return True

    def request_hint(
        self,
        *,
        student_id: int,
        submission_id: int,
        grading_run_id: int,
        request_key: str,
    ) -> tuple[HintRequest, QuotaStatusView]:
        req, quota_view = self.enqueue_hint(
            student_id=student_id,
            submission_id=submission_id,
            grading_run_id=grading_run_id,
            request_key=request_key,
        )
        if req.status != "processing":
            return req, quota_view
        processed = self.process_request(req.id)
        return processed or (req, quota_view)

    def get_hint_request(
        self,
        db: Session,
        *,
        student_id: int,
        request_id: int,
    ) -> tuple[HintRequest, QuotaStatusView] | None:
        now = self.now_fn()
        req = db.execute(
            select(HintRequest)
            .where(
                HintRequest.id == request_id,
                HintRequest.student_id == student_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if req is None:
            return None
        self._expire_request_if_needed(req, now=now)
        db.flush()
        quota_view = self.get_quota_status(
            db, student_id=student_id, problem_id=req.problem_id
        )
        db.commit()
        db.expunge(req)
        return req, quota_view

    def list_problem_hints(
        self,
        db: Session,
        *,
        student_id: int,
        problem_slug: str,
    ) -> tuple[QuotaStatusView, list[HintRequest]] | None:
        problem = db.scalar(
            select(Problem).where(
                Problem.slug == problem_slug,
                Problem.is_published.is_(True),
            )
        )
        if problem is None:
            return None
        quota_view = self.get_quota_status(
            db, student_id=student_id, problem_id=problem.id
        )
        hints = list(
            db.scalars(
                select(HintRequest)
                .where(
                    HintRequest.student_id == student_id,
                    HintRequest.problem_id == problem.id,
                    HintRequest.status == "succeeded",
                )
                .order_by(HintRequest.created_at.asc(), HintRequest.id.asc())
            )
        )
        return quota_view, hints
