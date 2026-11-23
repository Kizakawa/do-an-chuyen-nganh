"""Chạy nghiệm thu danh mục bài tập qua PostgreSQL và Judge0 thật.

Script tạo submission/grading_run như API, gọi chính GradingService và
đối chiếu reference/wrong sample với kết quả mong đợi trong catalog.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import sys
import time

from sqlalchemy import func, select

from app.config import get_settings
from app.database import SessionLocal
from app.judging.harness import PROTOCOL_VERSION
from app.judging.judge0 import Judge0Client, Judge0ConfigurationError
from app.judging.service import GradingService
from app.models import GradingRun, Problem, Student, Submission, TestCase, TestResult
from app.problems.catalog import ALL_PROBLEMS
from app.seed import seed_all


@dataclass(frozen=True)
class AcceptanceRow:
    source_key: str
    problem_slug: str
    variant: str
    submission_id: int
    grading_run_id: int
    run_status: str
    all_passed: bool | None
    passed_count: int
    expected_test_count: int
    failed_test_indexes: list[int]
    expected_failed_test_indexes: list[int]
    matched_expectation: bool
    technical_error_code: str | None
    duration_ms: int


def _enqueue(problem: Problem, student: Student, source_code: str) -> int:
    with SessionLocal.begin() as db:
        expected_count = db.scalar(
            select(func.count(TestCase.id)).where(
                TestCase.problem_id == problem.id,
                TestCase.test_version == problem.test_version,
            )
        )
        if not expected_count:
            raise RuntimeError(f"problem_has_no_tests:{problem.slug}")
        submission = Submission(
            student_id=student.id,
            problem_id=problem.id,
            source_code=source_code,
            problem_version=problem.version,
            test_version=problem.test_version,
        )
        db.add(submission)
        db.flush()
        run = GradingRun(
            submission_id=submission.id,
            problem_id=problem.id,
            test_version=problem.test_version,
            status="queued",
            all_passed=None,
            expected_test_count=expected_count,
            runtime_config={
                "harness_protocol": PROTOCOL_VERSION,
                "acceptance_runner": "catalog-v1",
            },
        )
        db.add(run)
        db.flush()
        return run.id


def _load_result(
    run_id: int,
    *,
    source_key: str,
    problem_slug: str,
    variant: str,
    expected_failed: list[int],
    duration_ms: int,
) -> AcceptanceRow:
    with SessionLocal() as db:
        run = db.get(GradingRun, run_id)
        if run is None:
            raise RuntimeError(f"grading_run_missing:{run_id}")
        rows = db.execute(
            select(TestResult, TestCase)
            .join(TestCase, TestCase.id == TestResult.test_case_id)
            .where(TestResult.grading_run_id == run_id)
            .order_by(TestCase.test_index)
        ).all()
        failed_indexes = [
            test.test_index for result, test in rows if result.status != "passed"
        ]
        passed_count = sum(result.status == "passed" for result, _ in rows)
        expected_failed_sorted = sorted(expected_failed)
        matched = (
            run.status == "completed"
            and len(rows) == run.expected_test_count
            and failed_indexes == expected_failed_sorted
            and run.all_passed == (not expected_failed_sorted)
        )
        return AcceptanceRow(
            source_key=source_key,
            problem_slug=problem_slug,
            variant=variant,
            submission_id=run.submission_id,
            grading_run_id=run.id,
            run_status=run.status,
            all_passed=run.all_passed,
            passed_count=passed_count,
            expected_test_count=run.expected_test_count,
            failed_test_indexes=failed_indexes,
            expected_failed_test_indexes=expected_failed_sorted,
            matched_expectation=matched,
            technical_error_code=run.technical_error_code,
            duration_ms=duration_ms,
        )


def _write_artifacts(rows: list[AcceptanceRow], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = output_dir / "judge0_catalog_acceptance.jsonl"
    csv_path = output_dir / "judge0_catalog_acceptance.csv"
    summary_path = output_dir / "judge0_catalog_summary.json"

    with jsonl_path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(asdict(row), ensure_ascii=False) + "\n")

    fieldnames = list(asdict(rows[0]).keys()) if rows else list(AcceptanceRow.__annotations__)
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            values = asdict(row)
            values["failed_test_indexes"] = json.dumps(values["failed_test_indexes"])
            values["expected_failed_test_indexes"] = json.dumps(
                values["expected_failed_test_indexes"]
            )
            writer.writerow(values)

    matched = sum(row.matched_expectation for row in rows)
    summary = {
        "generated_at": datetime.now(UTC).isoformat(),
        "total_cases": len(rows),
        "matched_cases": matched,
        "failed_cases": len(rows) - matched,
        "all_matched": bool(rows) and matched == len(rows),
        "reference_cases": sum(row.variant == "reference" for row in rows),
        "wrong_sample_cases": sum(row.variant.startswith("wrong:") for row in rows),
    }
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=20, choices=range(1, 21))
    parser.add_argument(
        "--variants",
        choices=["reference", "wrong", "both"],
        default="both",
    )
    parser.add_argument("--delay-seconds", type=float, default=0.5)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("artifacts/acceptance"),
    )
    parser.add_argument("--fail-fast", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    try:
        judge = Judge0Client(settings)
    except Judge0ConfigurationError as exc:
        print(f"CONFIGURATION ERROR: {exc}", file=sys.stderr)
        return 2

    with SessionLocal.begin() as db:
        seed_all(db)
    with SessionLocal() as db:
        pending_count = db.scalar(
            select(func.count(GradingRun.id)).where(
                GradingRun.status.in_(["queued", "running"])
            )
        )
        if pending_count:
            raise RuntimeError(
                "acceptance_requires_empty_grading_queue:"
                f"found_{pending_count}_pending_runs"
            )
        student = db.scalar(select(Student).where(Student.login_name == "demo"))
        if student is None:
            raise RuntimeError("demo_student_not_seeded")
        db.expunge(student)

    service = GradingService(
        SessionLocal,
        judge,
        stale_run_timeout_seconds=settings.grading_stale_run_timeout_seconds,
    )
    results: list[AcceptanceRow] = []

    for data in ALL_PROBLEMS[: args.limit]:
        with SessionLocal() as db:
            problem = db.scalar(select(Problem).where(Problem.slug == data["slug"]))
            if problem is None:
                raise RuntimeError(f"problem_not_seeded:{data['slug']}")
            db.expunge(problem)

        variants: list[tuple[str, str, list[int]]] = []
        if args.variants in {"reference", "both"}:
            variants.append(("reference", data["reference_code"], []))
        if args.variants in {"wrong", "both"}:
            variants.extend(
                (
                    f"wrong:{sample['name']}",
                    sample["source_code"],
                    list(sample["expected_failed_test_indexes"]),
                )
                for sample in data["wrong_samples"]
            )

        for variant, source_code, expected_failed in variants:
            run_id = _enqueue(problem, student, source_code)
            started = time.monotonic()
            processed = service.process_next()
            duration_ms = round((time.monotonic() - started) * 1000)
            if not processed:
                raise RuntimeError(f"worker_did_not_claim_run:{run_id}")
            row = _load_result(
                run_id,
                source_key=data["source_key"],
                problem_slug=data["slug"],
                variant=variant,
                expected_failed=expected_failed,
                duration_ms=duration_ms,
            )
            results.append(row)
            verdict = "PASS" if row.matched_expectation else "FAIL"
            print(
                f"{verdict} {row.source_key} {row.variant} "
                f"status={row.run_status} passed={row.passed_count}/"
                f"{row.expected_test_count} failed={row.failed_test_indexes} "
                f"duration_ms={row.duration_ms}",
                flush=True,
            )
            _write_artifacts(results, args.output_dir)
            if args.fail_fast and not row.matched_expectation:
                return 1
            if args.delay_seconds > 0:
                time.sleep(args.delay_seconds)

    return 0 if results and all(row.matched_expectation for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
