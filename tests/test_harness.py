import json
import os
import subprocess
import sys

import pytest

from app.judging.harness import (
    PROTOCOL_VERSION,
    HarnessProtocolError,
    build_harness,
    parse_harness_report,
)


def envelope(nonce: str, **values: object) -> str:
    return json.dumps({"protocol": PROTOCOL_VERSION, "nonce": nonce, **values})


def test_harness_embeds_code_as_base64_and_does_not_embed_expected() -> None:
    student_code = "def sum_numbers(numbers):\n    print('fake marker')\n    return 9"

    program = build_harness(student_code, "sum_numbers", [[2, 3, 4]])

    assert student_code not in program.source_code
    assert "fake marker" not in program.source_code
    assert "redirect_stdout" in program.source_code
    assert "expected_json" not in program.source_code
    # Chuỗi nonce gốc không tồn tại dưới dạng văn bản thuần trong mã nguồn Harness
    assert program.nonce not in program.source_code


def test_parse_value_report() -> None:
    report = parse_harness_report(
        envelope("nonce-1", kind="value", value=9), "nonce-1"
    )

    assert report.kind == "value"
    assert report.value == 9


def test_printed_fake_json_before_report_is_rejected() -> None:
    forged = '{"kind":"value","value":9}\n' + envelope(
        "nonce-1", kind="value", value=9
    )

    with pytest.raises(HarnessProtocolError, match="invalid_harness_json"):
        parse_harness_report(forged, "nonce-1")


def test_missing_output_from_early_exit_is_incomplete() -> None:
    with pytest.raises(HarnessProtocolError, match="missing_harness_output"):
        parse_harness_report(None, "nonce-1")


def test_wrong_nonce_is_rejected() -> None:
    with pytest.raises(HarnessProtocolError, match="invalid_harness_identity"):
        parse_harness_report(
            envelope("attacker-nonce", kind="value", value=9), "server-nonce"
        )


def test_harness_blocks_frame_nonce_theft_and_fd_os_exit_attack() -> None:
    """Chống tấn công đọc _RUN_NONCE qua sys._getframe(1) và ghi thẳng sys.__stdout__ + os._exit(0)."""
    exploit_code = (
        "import sys, os, json\n"
        "def sum_numbers(numbers):\n"
        "    real_nonce = sys._getframe(1).f_globals['_RUN_NONCE']\n"
        "    forged = {'protocol': 'python-poc-v1', 'nonce': real_nonce, 'kind': 'value', 'value': 9}\n"
        "    sys.__stdout__.write(json.dumps(forged) + '\\n')\n"
        "    sys.__stdout__.flush()\n"
        "    os._exit(0)\n"
    )
    program = build_harness(exploit_code, "sum_numbers", [[2, 3, 4]])
    completed = subprocess.run(
        [sys.executable, "-c", program.source_code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=5,
        check=False,
    )
    report = parse_harness_report(completed.stdout, program.nonce)
    assert report.kind == "runtime_error"
    assert report.value is None

    # Kịch bản 2: Bỏ qua _getframe, gọi thẳng sys.__stdout__.write + os._exit(0)
    fd_exit_exploit = (
        "import sys, os\n"
        "def sum_numbers(numbers):\n"
        "    sys.__stdout__.write('{\"forged\": true}\\n')\n"
        "    sys.__stdout__.flush()\n"
        "    os._exit(0)\n"
    )
    program2 = build_harness(fd_exit_exploit, "sum_numbers", [[2, 3, 4]])
    completed2 = subprocess.run(
        [sys.executable, "-c", program2.source_code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=5,
        check=False,
    )
    report2 = parse_harness_report(completed2.stdout, program2.nonce)
    assert report2.kind == "runtime_error"
    assert report2.error_type == "SystemExit"
    assert report2.value is None

    # Kịch bản 3 (Đòn 2 Vòng 2): Ghi thẳng vào fd=3 (os.write(3, ...)) và gọi os._exit(0)
    fd3_exploit = (
        "import os\n"
        "def sum_numbers(numbers):\n"
        "    os.write(3, b'{\"kind\":\"value\",\"value\":9}')\n"
        "    os._exit(0)\n"
    )
    program3 = build_harness(fd3_exploit, "sum_numbers", [[2, 3, 4]])
    completed3 = subprocess.run(
        [sys.executable, "-c", program3.source_code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=5,
        check=False,
    )
    report3 = parse_harness_report(completed3.stdout, program3.nonce)
    assert report3.kind == "runtime_error"
    assert report3.error_type == "RuntimeError"
    assert report3.value is None

    # Kịch bản 4: Quét bộ nhớ qua gc.get_objects() hoặc mở trực tiếp fd=3 qua open(3, "wb")
    gc_and_open_exploit = (
        "import gc\n"
        "def sum_numbers(numbers):\n"
        "    objs = gc.get_objects()\n"
        "    return len(objs)\n"
    )
    program4 = build_harness(gc_and_open_exploit, "sum_numbers", [[2, 3, 4]])
    completed4 = subprocess.run(
        [sys.executable, "-c", program4.source_code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=5,
        check=False,
    )
    report4 = parse_harness_report(completed4.stdout, program4.nonce)
    assert report4.kind == "runtime_error"
    assert report4.error_type == "RuntimeError"
    assert report4.value is None

    # Kịch bản 5 (1A): Monkey-patch json.dumps trong sys.modules để giả mạo kết quả khi hàm ném lỗi
    json_monkeypatch_exploit = (
        "import json\n"
        "_orig = json.dumps\n"
        "json.dumps = lambda obj, **kw: _orig(\n"
        "    {'protocol': obj['protocol'], 'nonce': obj['nonce'], 'kind': 'value', 'value': 9}\n"
        "    if isinstance(obj, dict) and 'nonce' in obj else obj, **kw\n"
        ")\n"
        "def sum_numbers(numbers):\n"
        "    raise RuntimeError('Code crash hoàn toàn nhưng vẫn lừa được Harness!')\n"
    )
    program5 = build_harness(json_monkeypatch_exploit, "sum_numbers", [[2, 3, 4]])
    completed5 = subprocess.run(
        [sys.executable, "-c", program5.source_code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=5,
        check=False,
    )
    report5 = parse_harness_report(completed5.stdout, program5.nonce)
    assert report5.kind == "runtime_error"
    assert report5.error_type == "RuntimeError"
    assert report5.value is None

    # Kịch bản 6 (1B): Leo ngược Call Stack qua Exception.__traceback__.tb_frame.f_back.f_locals
    tb_frame_exploit = (
        "def sum_numbers(numbers):\n"
        "    try:\n"
        "        1 / 0\n"
        "    except Exception as e:\n"
        "        loc = e.__traceback__.tb_frame.f_back.f_locals\n"
        "        nonce = bytes(a ^ b for a, b in zip(loc['_pad_a'], loc['_pad_b'])).decode('ascii')\n"
        "        import json\n"
        "        payload = (json.dumps({'protocol': 'python-poc-v1', 'nonce': nonce, 'kind': 'value', 'value': 9}) + '\\n').encode()\n"
        "        loc['_real_write'](loc['_saved_stdout_fd'], payload)\n"
    )
    program6 = build_harness(tb_frame_exploit, "sum_numbers", [[2, 3, 4]])
    completed6 = subprocess.run(
        [sys.executable, "-c", program6.source_code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=5,
        check=False,
    )
    report6 = parse_harness_report(completed6.stdout, program6.nonce)
    assert report6.kind == "runtime_error"
    assert report6.error_type == "KeyError"
    assert report6.value is None

    # Kịch bản 7: Monkey-patch sys.audit và xóa sạch f_globals của module cha
    sys_audit_sabotage = (
        "import sys\n"
        "sys.audit = lambda *a, **kw: None\n"
        "def sum_numbers(numbers):\n"
        "    try:\n"
        "        1 / 0\n"
        "    except Exception as e:\n"
        "        f = e.__traceback__.tb_frame.f_back\n"
        "        f.f_globals.clear()\n"
        "    return 999\n"
    )
    program7 = build_harness(sys_audit_sabotage, "sum_numbers", [[2, 3, 4]])
    completed7 = subprocess.run(
        [sys.executable, "-c", program7.source_code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=5,
        check=False,
    )
    report7 = parse_harness_report(completed7.stdout, program7.nonce)
    assert report7.kind == "value"
    assert report7.value == 999


def test_harness_allows_stdlib_imports_while_blocking_file_and_fd_open() -> None:
    """Cho phép import thư viện chuẩn (collections, re, heapq...) nhưng vẫn chặn tuyệt đối open() trực tiếp."""
    stdlib_code = (
        "from collections import Counter\n"
        "import re, heapq, bisect, decimal, statistics\n"
        "def sum_numbers(numbers):\n"
        "    counts = Counter(numbers)\n"
        "    assert re.match(r'^\\d+$', '123') is not None\n"
        "    heap = list(numbers)\n"
        "    heapq.heapify(heap)\n"
        "    _ = bisect.bisect_left([1, 2, 3], 2)\n"
        "    _ = (decimal.Decimal('1.5'), statistics.mean([1, 2]))\n"
        "    return sum(k * v for k, v in counts.items())\n"
    )
    program_ok = build_harness(stdlib_code, "sum_numbers", [[2, 3, 4]])
    completed_ok = subprocess.run(
        [sys.executable, "-c", program_ok.source_code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=5,
        check=False,
    )
    report_ok = parse_harness_report(completed_ok.stdout, program_ok.nonce)
    assert report_ok.kind == "value"
    assert report_ok.value == 9

    for exploit in (
        "def sum_numbers(numbers):\n    return open('pyproject.toml', 'r').read()\n",
        "import sys\ndef sum_numbers(numbers):\n    return open(sys.executable, 'rb').read(4)\n",
        "def sum_numbers(numbers):\n    return open(3, 'wb')\n",
    ):
        program_bad = build_harness(exploit, "sum_numbers", [[2, 3, 4]])
        completed_bad = subprocess.run(
            [sys.executable, "-c", program_bad.source_code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            timeout=5,
            check=False,
        )
        report_bad = parse_harness_report(completed_bad.stdout, program_bad.nonce)
        assert report_bad.kind == "runtime_error"
        assert report_bad.error_type == "RuntimeError"
        assert report_bad.value is None


def test_harness_blocks_json_encoder_patch_atexit_and_fake_importlib_open() -> None:
    """Chống monkey-patch json.JSONEncoder, callback atexit, giả mạo co_filename của importlib, os.listdir và posix."""
    cases = [
        (
            "import json\n"
            "class CustomEncoder(json.JSONEncoder):\n"
            "    def encode(self, o):\n"
            "        return '{\"kind\":\"value\",\"value\":9}'\n"
            "json.JSONEncoder = CustomEncoder\n"
            "def sum_numbers(numbers):\n"
            "    raise RuntimeError('test_marker')\n",
            "RuntimeError",
        ),
        (
            "import compileall\n"
            "def sum_numbers(numbers):\n"
            "    code = compile(\"open('pyproject.toml', 'r').read()\", '<frozen importlib._bootstrap_external>', 'exec')\n"
            "    exec(code)\n"
            "    return 0\n",
            "RuntimeError",
        ),
        (
            "import os\n"
            "def sum_numbers(numbers):\n"
            "    return len(os.listdir('.'))\n",
            "RuntimeError",
        ),
        (
            "import posix\n"
            "def sum_numbers(numbers):\n"
            "    return 0\n",
            "RuntimeError",
        ),
    ]
    for student_code, expected_err in cases:
        program = build_harness(student_code, "sum_numbers", [[2, 3, 4]])
        completed = subprocess.run(
            [sys.executable, "-c", program.source_code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            timeout=5,
            check=False,
        )
        report = parse_harness_report(completed.stdout, program.nonce)
        assert report.kind == "runtime_error"
        assert report.error_type == expected_err
        assert report.value is None

    atexit_code = (
        "import atexit\n"
        "def _after():\n"
        "    print('ATEXIT_MARKER')\n"
        "atexit.register(_after)\n"
        "def sum_numbers(numbers):\n"
        "    return sum(numbers)\n"
    )
    atexit_prog = build_harness(atexit_code, "sum_numbers", [[2, 3, 4]])
    atexit_run = subprocess.run(
        [sys.executable, "-c", atexit_prog.source_code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=5,
        check=False,
    )
    assert "ATEXIT_MARKER" not in atexit_run.stdout
    atexit_report = parse_harness_report(atexit_run.stdout, atexit_prog.nonce)
    assert atexit_report.kind == "value"
    assert atexit_report.value == 9


def test_harness_caps_oversized_return_value() -> None:
    oversized_code = "def sum_numbers(numbers):\n    return 'x' * 20000\n"
    program = build_harness(oversized_code, "sum_numbers", [[2, 3, 4]])
    completed = subprocess.run(
        [sys.executable, "-c", program.source_code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=5,
        check=False,
    )
    report = parse_harness_report(completed.stdout, program.nonce)
    assert report.kind == "runtime_error"
    assert report.error_type == "ValueError"
    assert report.message == "output_value_too_large"

    with pytest.raises(HarnessProtocolError, match="harness_output_too_large"):
        parse_harness_report("x" * 20000, "nonce-1")
