import base64
import json
import secrets
from dataclasses import dataclass
from typing import Any, Literal


PROTOCOL_VERSION = "python-poc-v1"
MAX_HARNESS_REPORT_CHARS = 16_384


class HarnessProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class HarnessProgram:
    source_code: str
    nonce: str


@dataclass(frozen=True)
class HarnessReport:
    kind: Literal["value", "syntax_error", "runtime_error"]
    value: Any = None
    error_type: str | None = None
    message: str | None = None


def build_harness(student_code: str, function_name: str, arguments: list[Any]) -> HarnessProgram:
    nonce = secrets.token_urlsafe(24)
    nonce_bytes = nonce.encode("ascii")
    pad_a = secrets.token_bytes(len(nonce_bytes))
    pad_b = bytes(a ^ b for a, b in zip(nonce_bytes, pad_a, strict=True))

    encoded_code = base64.b64encode(student_code.encode("utf-8")).decode("ascii")
    encoded_arguments = base64.b64encode(
        json.dumps(arguments, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")

    source = f'''import _json as _c_json
import base64 as _b64
import contextlib as _contextlib
import gc as _gc
import importlib._bootstrap_external as _ibe
import io as _io
import json as _json
import math as _math
import os as _os
import sys as _sys
import threading as _threading

def _init_c_audit_vault(_entry_code):
    _pad_a = {pad_a!r}
    _pad_b = {pad_b!r}
    _protocol = {PROTOCOL_VERSION!r}
    _real_write = _os.write
    _real_dup = _os.dup
    _real_dup2 = _os.dup2
    _real_close = _os.close
    _real_open = _os.open
    _real_os_exit = _os._exit
    _c_make_encoder = _c_json.make_encoder
    _c_encode_basestring = _c_json.encode_basestring
    _real_getframe = _sys._getframe
    _module_code = _real_getframe(1).f_code
    _normcase = _os.path.normcase
    _realpath = _os.path.realpath
    _isdir = _os.path.isdir
    _path_join = _os.path.join
    _sep = _os.sep
    _ver = f"{{_sys.version_info.major}}.{{_sys.version_info.minor}}"
    _prefix_candidates = []
    for _p in {{_sys.base_prefix, _sys.base_exec_prefix}}:
        if not _p:
            continue
        for _sub in ("Lib", "DLLs", _path_join("lib", f"python{{_ver}}"), _path_join("lib64", f"python{{_ver}}")):
            _full = _path_join(_p, _sub)
            if _isdir(_full):
                _prefix_candidates.append(_normcase(_realpath(_full)) + _sep)
    _stdlib_prefixes = tuple(dict.fromkeys(_prefix_candidates))
    _valid_importlib_codes = frozenset(
        _code
        for _obj in vars(_ibe).values()
        for _member in ([_obj] + (list(vars(_obj).values()) if isinstance(_obj, type) else []))
        for _fn in (getattr(_member, "__func__", _member),)
        for _code in (getattr(_fn, "__code__", None),)
        if _code is not None
    )

    def _no_default(_o):
        raise TypeError("Object is not JSON serializable")

    def _vault_json_dumps(_obj):
        _enc = _c_make_encoder(
            {{}},
            _no_default,
            _c_encode_basestring,
            None,
            ":",
            ",",
            False,
            False,
            False,
        )
        return "".join(_enc(_obj, 0))

    _saved_stdout_fd = _real_dup(1)
    _devnull_fd = _real_open(_os.devnull, _os.O_WRONLY)
    try:
        _real_dup2(_devnull_fd, 1)
        _real_dup2(_devnull_fd, 2)
    finally:
        _real_close(_devnull_fd)

    _state = {{"guard": False, "emitted": False}}
    _hook_tls = _threading.local()

    _BLOCKED_EVENTS = frozenset({{
        "sys._getframe",
        "sys._current_frames",
        "sys._current_exceptions",
        "gc.get_objects",
        "gc.get_referrers",
        "gc.get_referents",
        "os.system",
        "os.spawn",
        "os.posix_spawn",
        "os.exec",
        "os.fork",
        "os.scandir",
        "os.unlink",
        "os.remove",
        "os.rmdir",
        "os.mkdir",
        "os.rename",
        "os.replace",
        "os.symlink",
        "os.link",
        "os.chmod",
        "os.chown",
        "os.truncate",
        "os.putenv",
        "os.unsetenv",
        "subprocess.Popen",
        "ctypes.dlopen",
        "ctypes.dlsym",
        "_thread.start_new_thread",
        "socket.__new__",
        "socket.connect",
        "socket.bind",
        "urllib.Request",
    }})

    _BLOCKED_MODULES = frozenset({{
        "posix",
        "nt",
        "ctypes",
        "_ctypes",
        "subprocess",
        "_posixsubprocess",
        "socket",
        "_socket",
        "multiprocessing",
        "_multiprocessing",
        "_thread",
        "fcntl",
        "pty",
        "resource",
        "signal",
    }})

    def _audit_hook(_event, _args):
        if getattr(_hook_tls, "in_hook", False):
            return
        if _event == "harness.enter":
            _hook_tls.in_hook = True
            try:
                _caller = _real_getframe(1)
                if (
                    _caller.f_code is _entry_code
                    and _caller.f_back is not None
                    and _caller.f_back.f_back is None
                    and _caller.f_back.f_code is _module_code
                ):
                    _state["guard"] = True
                    return
            finally:
                _hook_tls.in_hook = False
            raise RuntimeError("restricted_harness_event")

        if _event == "harness.emit":
            _hook_tls.in_hook = True
            try:
                _caller = _real_getframe(1)
                if (
                    not _state["emitted"]
                    and _caller.f_code is _entry_code
                    and _caller.f_back is not None
                    and _caller.f_back.f_back is None
                    and _caller.f_back.f_code is _module_code
                    and len(_args) == 1
                    and isinstance(_args[0], dict)
                ):
                    _state["emitted"] = True
                    _report = _args[0]
                    _nonce = bytes(_a ^ _b for _a, _b in zip(_pad_a, _pad_b)).decode("ascii")
                    _envelope = {{"protocol": _protocol, "nonce": _nonce, **_report}}
                    _payload = (_vault_json_dumps(_envelope) + "\\n").encode("utf-8")
                    _real_write(_saved_stdout_fd, _payload)
                    _real_close(_saved_stdout_fd)
                    _real_os_exit(0)
                    return
            finally:
                _hook_tls.in_hook = False
            raise RuntimeError("restricted_harness_event")

        if _state["guard"] and _event == "open":
            _hook_tls.in_hook = True
            try:
                _file = _args[0] if len(_args) > 0 else None
                _mode = _args[1] if len(_args) > 1 else None
                if isinstance(_file, str) and _mode in ("r", "rb", None):
                    _norm_file = _normcase(_realpath(_file))
                    if (
                        _norm_file.startswith(_stdlib_prefixes)
                        and "site-packages" not in _norm_file
                        and "dist-packages" not in _norm_file
                        and _real_getframe(1).f_code in _valid_importlib_codes
                    ):
                        return
            finally:
                _hook_tls.in_hook = False
            raise RuntimeError("restricted_runtime_event:open")

        if _state["guard"] and _event == "os.listdir":
            _hook_tls.in_hook = True
            try:
                if _real_getframe(1).f_code in _valid_importlib_codes:
                    return
            finally:
                _hook_tls.in_hook = False
            raise RuntimeError("restricted_runtime_event:os.listdir")

        if _state["guard"] and _event == "import":
            _mod_name = _args[0] if len(_args) > 0 and isinstance(_args[0], str) else ""
            _mod_root = _mod_name.split(".")[0]
            if _mod_root in _BLOCKED_MODULES:
                raise RuntimeError(f"restricted_runtime_event:import:{{_mod_root}}")

        if _state["guard"] and _event in _BLOCKED_EVENTS:
            raise RuntimeError(f"restricted_runtime_event:{{_event}}")

    _sys.addaudithook(_audit_hook)

def _execute_harness(
    _c_make_enc=_c_json.make_encoder,
    _c_enc_str=_c_json.encode_basestring,
    _isnan=_math.isnan,
    _isinf=_math.isinf,
):
    _b64decode = _b64.b64decode
    _json_loads = _json.loads
    _max_report_chars = {MAX_HARNESS_REPORT_CHARS}
    _student_code = _b64decode({encoded_code!r}).decode("utf-8")
    _arguments = _json_loads(_b64decode({encoded_arguments!r}).decode("utf-8"))
    _function_name = {function_name!r}
    _captured_stdout = _io.StringIO()
    _captured_stderr = _io.StringIO()
    _namespace = {{"__name__": "student_submission"}}

    def _blocked_syscall(*_args, **_kwargs):
        raise RuntimeError("restricted_os_operation")

    def _trapped_os_exit(_code=0):
        raise SystemExit(_code)

    def _sanitize_json_value(_val, _depth=0):
        if _depth > 64:
            raise ValueError("max_json_depth_exceeded")
        if _val is None:
            return None
        if isinstance(_val, bool):
            return bool(_val)
        if isinstance(_val, int):
            return int(_val)
        if isinstance(_val, float):
            if _isnan(_val) or _isinf(_val):
                raise ValueError("Out of range float values are not JSON compliant")
            return float(_val)
        if isinstance(_val, str):
            return str.__str__(_val) if type(_val) is not str else _val
        if isinstance(_val, (list, tuple)):
            return [_sanitize_json_value(_item, _depth + 1) for _item in _val]
        if isinstance(_val, dict):
            _out = {{}}
            for _k, _v in _val.items():
                if _k is None:
                    _sk = "null"
                elif isinstance(_k, bool):
                    _sk = "true" if _k else "false"
                elif isinstance(_k, int):
                    _sk = str(int(_k))
                elif isinstance(_k, float):
                    if _isnan(_k) or _isinf(_k):
                        raise ValueError("Out of range float values are not JSON compliant")
                    _sk = str(float(_k))
                elif isinstance(_k, str):
                    _sk = str.__str__(_k) if type(_k) is not str else _k
                else:
                    raise TypeError("dict key must be str, int, float, bool or None")
                _out[_sk] = _sanitize_json_value(_v, _depth + 1)
            return _out
        raise TypeError(f"Object of type {{type(_val).__name__}} is not JSON serializable")

    try:
        with _contextlib.redirect_stdout(_captured_stdout), _contextlib.redirect_stderr(_captured_stderr):
            try:
                _compiled = compile(_student_code, "<student_submission>", "exec")
            except SyntaxError as _exc:
                return {{
                    "kind": "syntax_error",
                    "error_type": "SyntaxError",
                    "message": str(_exc)[:1000],
                }}
            for _mod_name in ("os", "posix", "nt"):
                _mod = _sys.modules.get(_mod_name)
                if _mod is not None:
                    for _attr in ("write", "writev", "pwrite", "pwritev", "dup", "dup2", "close", "open", "fdopen"):
                        if hasattr(_mod, _attr):
                            try:
                                setattr(_mod, _attr, _blocked_syscall)
                            except (AttributeError, TypeError):
                                pass
            _os._exit = _trapped_os_exit
            _gc.get_objects = _blocked_syscall
            _gc.get_referrers = _blocked_syscall
            _gc.get_referents = _blocked_syscall
            try:
                exec(_compiled, _namespace, _namespace)
                _function = _namespace.get(_function_name)
                if not callable(_function):
                    raise TypeError("required_function_not_found")
                _value = _function(*_arguments)
                _clean_value = _sanitize_json_value(_value)
                _enc = _c_make_enc(
                    {{}},
                    None,
                    _c_enc_str,
                    None,
                    ":",
                    ",",
                    False,
                    False,
                    False,
                )
                _serialized = "".join(_enc(_clean_value, 0))
                if len(_serialized) > _max_report_chars:
                    raise ValueError("output_value_too_large")
                return {{"kind": "value", "value": _clean_value}}
            except BaseException as _exc:
                return {{
                    "kind": "runtime_error",
                    "error_type": type(_exc).__name__,
                    "message": str(_exc)[:1000],
                }}
    except BaseException as _runner_exc:
        return {{
            "kind": "runner_error",
            "error_type": type(_runner_exc).__name__,
            "message": "runner_failed",
        }}

def _run_harness_entry(_audit=_sys.audit, _exec=_execute_harness):
    _audit("harness.enter")
    _res = _exec()
    _audit("harness.emit", _res)

_init_c_audit_vault(_run_harness_entry.__code__)
del _init_c_audit_vault, _execute_harness, _c_json, _ibe, _math, _threading
_gc.collect()
_run_harness_entry()
'''
    return HarnessProgram(source_code=source, nonce=nonce)


def parse_harness_report(stdout: str | None, expected_nonce: str) -> HarnessReport:
    if not stdout:
        raise HarnessProtocolError("missing_harness_output")
    if len(stdout) > MAX_HARNESS_REPORT_CHARS + 1024:
        raise HarnessProtocolError("harness_output_too_large")
    try:
        payload = json.loads(stdout.strip())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HarnessProtocolError("invalid_harness_json") from exc
    if not isinstance(payload, dict):
        raise HarnessProtocolError("invalid_harness_envelope")
    if payload.get("protocol") != PROTOCOL_VERSION or payload.get("nonce") != expected_nonce:
        raise HarnessProtocolError("invalid_harness_identity")
    kind = payload.get("kind")
    if kind == "runner_error":
        raise HarnessProtocolError("runner_failed")
    if kind not in {"value", "syntax_error", "runtime_error"}:
        raise HarnessProtocolError("invalid_harness_kind")
    if kind == "value" and "value" not in payload:
        raise HarnessProtocolError("missing_harness_value")
    return HarnessReport(
        kind=kind,
        value=payload.get("value"),
        error_type=payload.get("error_type"),
        message=payload.get("message"),
    )
