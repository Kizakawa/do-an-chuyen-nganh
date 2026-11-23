import copy
import csv
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import statistics
import subprocess
import sys
from typing import Any, Literal

from app.hint_context.schemas import (
    GradingContext,
    HiddenSummaryEvidence,
    HintContextV1,
    ProblemContext,
    PublicEvidence,
)
from app.hints.adapters import LLMServiceInterface
from app.hints.service import generate_and_validate_hint_core
from app.judging.comparison import values_equal
from app.judging.harness import HarnessProtocolError, build_harness, parse_harness_report
from app.problems.catalog import ALL_PROBLEMS


@dataclass(frozen=True)
class BenchmarkSampleRecord:
    sample_id: str
    case_id: str
    problem_slug: str
    source_key: str
    model_id: str
    provider: str
    prompt_version: str
    level: Literal["KM", "KH"]
    temperature: float
    evidence_count: int
    attempt_count: int
    any_leaked: bool
    leak_reasons: list[str]
    max_token_overlap: float | None
    status: Literal["succeeded", "failed"]
    final_hint: str | None
    error_code: str | None
    total_latency_ms: int
    generation_log: list[dict[str, Any]]


@dataclass(frozen=True)
class RubricRow:
    sample_id: str
    case_id: str
    level: Literal["KM", "KH"]
    r1_cause: int  # 0..2
    r2_evidence: int  # 0..2
    r3_level: int  # 0..2
    r4_usefulness: int  # 0..2
    r5_no_leak: int  # 0..2
    rater_id: str = "consensus"
    notes: str = ""
    rater_a_scores: tuple[int, int, int, int, int] | None = None
    rater_b_scores: tuple[int, int, int, int, int] | None = None


def _sanitize_csv_cell(value: str) -> str:
    if value and value[0] in ("=", "+", "-", "@", "\t", "\r"):
        return f"'{value}"
    return value


def _simulate_local_context_for_sample(
    problem: dict[str, Any],
    wrong_sample: dict[str, Any],
    *,
    problem_id: int,
    submission_id: int,
    grading_run_id: int,
    max_evidence: int = 2,
) -> HintContextV1:
    """Tạo HintContextV1 cho bài nộp sai mẫu trong tập catalog để chạy benchmark ngoại tuyến."""
    use_isolated_subprocess = problem.get("source_name", "self_authored") != "self_authored"
    namespace: dict[str, Any] = {"__name__": "benchmark_sample"}
    compile_error: str | None = None
    exec_error: str | None = None
    func: Any = None

    if not use_isolated_subprocess:
        try:
            compiled = compile(wrong_sample["source_code"], "<benchmark_sample>", "exec")
        except SyntaxError as exc:
            compile_error = f"SyntaxError: {exc}"
        else:
            try:
                exec(compiled, namespace, namespace)
                candidate = namespace.get(problem["function_name"])
                if not callable(candidate):
                    exec_error = "TypeError: required_function_not_found"
                else:
                    func = candidate
            except Exception as exc:
                exec_error = f"{type(exc).__name__}: {exc}"

    passed_count = 0
    failed_count = 0
    public_failures: list[PublicEvidence] = []
    hidden_failed_pairs: list[tuple[str, dict[str, Any]]] = []

    for test in problem["tests"]:
        if use_isolated_subprocess:
            program = build_harness(
                wrong_sample["source_code"],
                problem["function_name"],
                copy.deepcopy(test["args"]),
            )
            try:
                completed = subprocess.run(
                    [sys.executable, "-I", "-c", program.source_code],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=5,
                    check=False,
                )
                report = parse_harness_report(completed.stdout, program.nonce)
                if report.kind == "syntax_error":
                    actual = None
                    passed = False
                    failure_type = "syntax_error"
                    error_summary = f"SyntaxError: {report.message or ''}"
                elif report.kind == "runtime_error":
                    actual = None
                    passed = False
                    failure_type = "runtime_error"
                    error_summary = f"{report.error_type or 'RuntimeError'}: {report.message or ''}"
                else:
                    actual = report.value
                    passed = values_equal(actual, test["expected"])
                    failure_type = "passed" if passed else "wrong_answer"
                    error_summary = None
            except (subprocess.TimeoutExpired, HarnessProtocolError) as exc:
                actual = None
                passed = False
                failure_type = "runtime_error"
                error_summary = f"{type(exc).__name__}: {exc}"
        elif compile_error is not None:
            actual = None
            passed = False
            failure_type = "syntax_error"
            error_summary = compile_error
        elif exec_error is not None or func is None:
            actual = None
            passed = False
            failure_type = "runtime_error"
            error_summary = exec_error or "required_function_not_found"
        else:
            try:
                # Sao chép sâu đối số để hàm sinh viên có thao tác in-place (.sort(), .pop())
                # không làm hỏng dữ liệu gốc của ALL_PROBLEMS trong bộ nhớ.
                args_copy = copy.deepcopy(test["args"])
                actual = func(*args_copy)
                passed = values_equal(actual, test["expected"])
                failure_type = "passed" if passed else "wrong_answer"
                error_summary = None
            except Exception as exc:
                actual = None
                passed = False
                failure_type = "runtime_error"
                error_summary = f"{type(exc).__name__}: {exc}"

        if passed:
            passed_count += 1
        else:
            failed_count += 1
            if test["is_public"]:
                if len(public_failures) < max_evidence:
                    public_failures.append(
                        PublicEvidence(
                            test_index=test["test_index"],
                            failure_type=failure_type,  # type: ignore[arg-type]
                            args=copy.deepcopy(test["args"]),
                            expected=copy.deepcopy(test["expected"]),
                            actual=actual,
                            error_summary=error_summary,
                        )
                    )
            else:
                hidden_failed_pairs.append((failure_type, test))

    hidden_categories: list[str] = []
    hidden_statuses: list[str] = []

    if public_failures:
        evidence = public_failures
    else:
        for failure_type, test in hidden_failed_pairs:
            purpose = test.get("purpose")
            if purpose and purpose not in hidden_categories:
                hidden_categories.append(purpose)
            if failure_type and failure_type not in hidden_statuses:
                hidden_statuses.append(failure_type)
        evidence = [
            HiddenSummaryEvidence(
                summary=(
                    "Bài nộp chưa đạt một hoặc nhiều test ẩn; chi tiết đầu vào, "
                    "kết quả mong đợi và kết quả thực tế đã được lược bỏ."
                )
            )
        ]

    return HintContextV1(
        problem_id=problem_id,
        problem_version=problem["version"],
        submission_id=submission_id,
        grading_run_id=grading_run_id,
        test_version=problem["test_version"],
        problem=ProblemContext(
            statement=problem["statement"],
            function_signature=problem["function_signature"],
        ),
        student_code=wrong_sample["source_code"],
        grading=GradingContext(
            status="completed",
            all_passed=False,
            passed_count=passed_count,
            failed_count=failed_count,
        ),
        evidence=evidence,
        hidden_failure_categories=hidden_categories[:max_evidence],
        hidden_failure_statuses=hidden_statuses,
    )


def run_offline_benchmark(
    adapter: LLMServiceInterface,
    *,
    problems: list[dict[str, Any]] | None = None,
    levels: tuple[Literal["KM", "KH"], ...] = ("KM", "KH"),
    max_retries: int = 2,
    token_overlap_threshold: float = 0.75,
    temperature: float = 0.2,
    timeout_seconds: float = 15.0,
) -> list[BenchmarkSampleRecord]:
    """Chạy benchmark ngoại tuyến trên danh sách bài tập và các mẫu mã sai (wrong_samples)."""
    selected_problems = problems if problems is not None else ALL_PROBLEMS
    records: list[BenchmarkSampleRecord] = []
    sample_counter = 1

    for prob_idx, problem in enumerate(selected_problems, start=1):
        for wrong_idx, wrong_sample in enumerate(problem["wrong_samples"], start=1):
            case_id = f"{problem['source_key']}_{wrong_sample['name']}"
            context = _simulate_local_context_for_sample(
                problem,
                wrong_sample,
                problem_id=prob_idx,
                submission_id=prob_idx * 100 + wrong_idx,
                grading_run_id=prob_idx * 100 + wrong_idx,
            )
            prior_sample_hints: list[str] = []
            for level in levels:
                sample_id = f"BM-{sample_counter:03d}"
                sample_counter += 1
                level_context = context.model_copy(
                    update={"previous_hints": list(prior_sample_hints)}
                )
                core = generate_and_validate_hint_core(
                    level_context,
                    level,
                    adapter,
                    max_retries=max_retries,
                    reference_code=problem["reference_code"],
                    token_overlap_threshold=token_overlap_threshold,
                    temperature=temperature,
                    timeout_seconds=timeout_seconds,
                )
                if core.valid_hint is not None:
                    prior_sample_hints.append(
                        f"[Cùng bản nộp hiện tại · mức {level}] {core.valid_hint}"
                    )
                leak_reasons: list[str] = []
                overlaps: list[float] = []
                total_latency = 0
                for entry in core.generation_log:
                    total_latency += int(entry.get("latency_ms") or 0)
                    for reason in entry.get("leak_reasons") or []:
                        if reason not in leak_reasons:
                            leak_reasons.append(reason)
                    if entry.get("token_overlap") is not None:
                        overlaps.append(float(entry["token_overlap"]))

                records.append(
                    BenchmarkSampleRecord(
                        sample_id=sample_id,
                        case_id=case_id,
                        problem_slug=problem["slug"],
                        source_key=problem["source_key"],
                        model_id=core.model_id,
                        provider=core.provider,
                        prompt_version=core.prompt_version,
                        level=level,
                        temperature=temperature,
                        evidence_count=len(context.evidence),
                        attempt_count=len(core.generation_log),
                        any_leaked=core.any_leaked,
                        leak_reasons=leak_reasons,
                        max_token_overlap=max(overlaps) if overlaps else None,
                        status="succeeded" if core.valid_hint is not None else "failed",
                        final_hint=core.valid_hint,
                        error_code=core.error_code,
                        total_latency_ms=total_latency,
                        generation_log=core.generation_log,
                    )
                )
    return records


def write_benchmark_artifacts(
    records: list[BenchmarkSampleRecord],
    output_dir: Path,
) -> tuple[Path, Path]:
    """Ghi kết quả sinh ra file JSONL và tạo bảng chấm điểm Rubric R1–R5 định dạng CSV."""
    output_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = output_dir / "benchmark_generations.jsonl"
    csv_path = output_dir / "rubric_evaluation_sheet.csv"

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")

    # Phiếu chấm mù 2 giám khảo độc lập (Blind Dual-Rater Evaluation Sheet):
    # - Ẩn hoàn toàn model_id và prompt_version để tránh thiên lệch nhận thức (Observer Bias).
    # - Tách cột điểm độc lập cho Giám khảo A (r1_a..r5_a), Giám khảo B (r1_b..r5_b) và cột điểm đồng thuận (r1_cause..r5_no_leak).
    fieldnames = [
        "sample_id",
        "case_id",
        "problem_slug",
        "level",
        "status",
        "final_hint",
        "r1_a",
        "r2_a",
        "r3_a",
        "r4_a",
        "r5_a",
        "r1_b",
        "r2_b",
        "r3_b",
        "r4_b",
        "r5_b",
        "r1_cause",
        "r2_evidence",
        "r3_level",
        "r4_usefulness",
        "r5_no_leak",
        "leak_flag",
        "rater_1_id",
        "rater_2_id",
        "disagreement_notes",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            writer.writerow(
                {
                    "sample_id": _sanitize_csv_cell(record.sample_id),
                    "case_id": _sanitize_csv_cell(record.case_id),
                    "problem_slug": _sanitize_csv_cell(record.problem_slug),
                    "level": record.level,
                    "status": record.status,
                    "final_hint": _sanitize_csv_cell(record.final_hint or ""),
                    "r1_a": "",
                    "r2_a": "",
                    "r3_a": "",
                    "r4_a": "",
                    "r5_a": "",
                    "r1_b": "",
                    "r2_b": "",
                    "r3_b": "",
                    "r4_b": "",
                    "r5_b": "",
                    "r1_cause": "",
                    "r2_evidence": "",
                    "r3_level": "",
                    "r4_usefulness": "",
                    "r5_no_leak": "",
                    "leak_flag": "",
                    "rater_1_id": "",
                    "rater_2_id": "",
                    "disagreement_notes": "",
                }
            )
    return jsonl_path, csv_path


def load_benchmark_jsonl(jsonl_path: Path) -> list[BenchmarkSampleRecord]:
    """Đọc lại danh sách BenchmarkSampleRecord từ file benchmark_generations.jsonl."""
    records: list[BenchmarkSampleRecord] = []
    with jsonl_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            item = json.loads(stripped)
            records.append(BenchmarkSampleRecord(**item))
    return records


def load_rubric_csv(csv_path: Path) -> list[RubricRow]:
    """Đọc bảng điểm chấm chéo R1–R5 (hỗ trợ cả cột 2 giám khảo độc lập r1_a..r5_a / r1_b..r5_b và cột đồng thuận)."""
    rows: list[RubricRow] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            a_vals = [(raw.get(f"r{i}_a") or "").strip() for i in range(1, 6)]
            b_vals = [(raw.get(f"r{i}_b") or "").strip() for i in range(1, 6)]
            rater_a: tuple[int, int, int, int, int] | None = None
            rater_b: tuple[int, int, int, int, int] | None = None
            if all(a_vals):
                rater_a = (int(a_vals[0]), int(a_vals[1]), int(a_vals[2]), int(a_vals[3]), int(a_vals[4]))
            if all(b_vals):
                rater_b = (int(b_vals[0]), int(b_vals[1]), int(b_vals[2]), int(b_vals[3]), int(b_vals[4]))

            r1 = (raw.get("r1_cause") or "").strip()
            r2 = (raw.get("r2_evidence") or "").strip()
            r3 = (raw.get("r3_level") or "").strip()
            r4 = (raw.get("r4_usefulness") or "").strip()
            r5 = (raw.get("r5_no_leak") or "").strip()

            if not (r1 and r2 and r3 and r4 and r5):
                if rater_a is not None and rater_b is not None:
                    # Nếu chưa điền cột đồng thuận nhưng đã có đủ điểm của 2 giám khảo, lấy min bảo thủ cho r5 và làm tròn trung bình cho r1..r4
                    c_scores = [
                        int(round((rater_a[i] + rater_b[i]) / 2)) if i < 4 else min(rater_a[4], rater_b[4])
                        for i in range(5)
                    ]
                else:
                    continue
            else:
                c_scores = [int(r1), int(r2), int(r3), int(r4), int(r5)]

            level_val: Literal["KM", "KH"] = (
                "KH" if (raw.get("level") or "").strip().upper() == "KH" else "KM"
            )
            rows.append(
                RubricRow(
                    sample_id=(raw.get("sample_id") or "").strip(),
                    case_id=(raw.get("case_id") or "").strip(),
                    level=level_val,
                    r1_cause=c_scores[0],
                    r2_evidence=c_scores[1],
                    r3_level=c_scores[2],
                    r4_usefulness=c_scores[3],
                    r5_no_leak=c_scores[4],
                    rater_id=(raw.get("rater_1_id") or "consensus").strip(),
                    notes=(raw.get("disagreement_notes") or "").strip(),
                    rater_a_scores=rater_a,
                    rater_b_scores=rater_b,
                )
            )
    return rows


def compute_cohens_kappa(pairs: list[tuple[int, int]]) -> float | None:
    """Tính hệ số đồng thuận liên giám khảo Cohen's Kappa (kappa = (p_o - p_e) / (1 - p_e)) trên thang 0..2."""
    if not pairs:
        return None
    n = len(pairs)
    categories = (0, 1, 2)
    p_o = sum(1 for a, b in pairs if a == b) / n
    p_e = sum(
        (sum(1 for a, _ in pairs if a == c) / n)
        * (sum(1 for _, b in pairs if b == c) / n)
        for c in categories
    )
    if abs(1.0 - p_e) < 1e-9:
        return 1.0
    return round((p_o - p_e) / (1.0 - p_e), 4)


def _percentile_95(values: list[int]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1)))))
    return float(ordered[index])


def compute_m1_to_m5(
    records: list[BenchmarkSampleRecord],
    rubric_rows: list[RubricRow] | None = None,
) -> dict[str, Any]:
    """Tính bộ chỉ số M1–M5 theo đúng đặc tả Mục 2.6.6 Chương 2."""
    total_n = len(records)
    succeeded = [rec for rec in records if rec.status == "succeeded"]
    failed = [rec for rec in records if rec.status != "succeeded"]
    total_s = len(succeeded)

    succeeded_latencies = [rec.total_latency_ms for rec in succeeded]
    failed_latencies = [rec.total_latency_ms for rec in failed]
    guardrail_flagged_count = sum(1 for rec in records if rec.any_leaked)

    metrics: dict[str, Any] = {
        "N_total_requests": total_n,
        "S_succeeded_requests": total_s,
        "success_rate_S_over_N": round(total_s / total_n, 4) if total_n > 0 else None,
        "guardrail_flagged_requests": guardrail_flagged_count,
        "guardrail_flagged_rate_over_N": (
            round(guardrail_flagged_count / total_n, 4) if total_n > 0 else None
        ),
        "M1_cause_accuracy": None,
        "M1_cause_accuracy_KM": None,
        "M1_cause_accuracy_KH": None,
        "M2_level_compliance": None,
        "M3_leakage_rate": None,
        "M3_leakage_rate_over_N": None,
        "M3_borderline_r5_eq_1_count": None,
        "M4_usefulness_mean": None,
        "M4_usefulness_distribution": None,
        "inter_rater_exact_agreement": None,
        "inter_rater_cohens_kappa": None,
        "M5_latency_ms": {
            "succeeded_median_ms": (
                float(statistics.median(succeeded_latencies))
                if succeeded_latencies
                else None
            ),
            "succeeded_p95_ms": _percentile_95(succeeded_latencies),
            "failed_median_ms": (
                float(statistics.median(failed_latencies))
                if failed_latencies
                else None
            ),
            "failed_p95_ms": _percentile_95(failed_latencies),
        },
    }

    if not rubric_rows or total_s == 0:
        return metrics

    succeeded_ids = {rec.sample_id for rec in succeeded}
    valid_rubric = [row for row in rubric_rows if row.sample_id in succeeded_ids]
    if not valid_rubric:
        return metrics

    denom = len(valid_rubric)
    km_rows = [row for row in valid_rubric if row.level == "KM"]
    kh_rows = [row for row in valid_rubric if row.level == "KH"]
    leaked_in_delivered = sum(1 for row in valid_rubric if row.r5_no_leak == 0)

    metrics["M1_cause_accuracy"] = round(
        sum(1 for row in valid_rubric if row.r1_cause == 2) / denom, 4
    )
    metrics["M1_cause_accuracy_KM"] = (
        round(sum(1 for row in km_rows if row.r1_cause == 2) / len(km_rows), 4)
        if km_rows
        else None
    )
    metrics["M1_cause_accuracy_KH"] = (
        round(sum(1 for row in kh_rows if row.r1_cause == 2) / len(kh_rows), 4)
        if kh_rows
        else None
    )
    metrics["M2_level_compliance"] = round(
        sum(1 for row in valid_rubric if row.r3_level == 2) / denom, 4
    )
    metrics["M3_leakage_rate"] = round(leaked_in_delivered / denom, 4)
    metrics["M3_leakage_rate_over_N"] = (
        round(leaked_in_delivered / total_n, 4) if total_n > 0 else None
    )
    metrics["M3_borderline_r5_eq_1_count"] = sum(
        1 for row in valid_rubric if row.r5_no_leak == 1
    )
    metrics["M4_usefulness_mean"] = round(
        sum(row.r4_usefulness for row in valid_rubric) / denom, 4
    )
    metrics["M4_usefulness_distribution"] = {
        "0": sum(1 for row in valid_rubric if row.r4_usefulness == 0),
        "1": sum(1 for row in valid_rubric if row.r4_usefulness == 1),
        "2": sum(1 for row in valid_rubric if row.r4_usefulness == 2),
    }

    rater_pairs: list[tuple[int, int]] = []
    for row in valid_rubric:
        if row.rater_a_scores is not None and row.rater_b_scores is not None:
            for a_val, b_val in zip(row.rater_a_scores, row.rater_b_scores, strict=True):
                rater_pairs.append((a_val, b_val))
    if rater_pairs:
        metrics["inter_rater_exact_agreement"] = round(
            sum(1 for a_val, b_val in rater_pairs if a_val == b_val) / len(rater_pairs),
            4,
        )
        metrics["inter_rater_cohens_kappa"] = compute_cohens_kappa(rater_pairs)

    return metrics
