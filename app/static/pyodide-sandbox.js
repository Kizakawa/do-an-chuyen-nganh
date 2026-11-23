(() => {
  let sampleWorker = null;
  let activeRunId = null;

  function createPyodideWorker() {
    const workerSource = `
      let pyodideReadyPromise = null;
      let sandboxLocked = false;

      function getPyodide() {
        if (!pyodideReadyPromise) {
          importScripts("https://cdn.jsdelivr.net/pyodide/v0.26.2/full/pyodide.js");
          pyodideReadyPromise = self.loadPyodide({
            indexURL: "https://cdn.jsdelivr.net/pyodide/v0.26.2/full/"
          });
        }
        return pyodideReadyPromise;
      }

      function lockWorkerNetworkApis() {
        if (sandboxLocked) return;
        sandboxLocked = true;
        const blocked = () => {
          throw new Error("network_disabled_in_client_sandbox");
        };
        for (const apiName of [
          "fetch",
          "XMLHttpRequest",
          "WebSocket",
          "EventSource",
          "BroadcastChannel",
          "importScripts"
        ]) {
          try {
            Object.defineProperty(self, apiName, {
              value: blocked,
              writable: false,
              configurable: false
            });
          } catch (_) {}
        }
      }

      self.onmessage = async (event) => {
        const { sourceCode, functionName, examples } = event.data;
        try {
          const pyodide = await getPyodide();
          lockWorkerNetworkApis();
          self.postMessage({ type: "ready" });
          pyodide.globals.set("_USER_CODE", sourceCode);
          pyodide.globals.set("_FUNC_NAME", functionName);
          pyodide.globals.set("_TESTS_JSON", JSON.stringify(examples));
          const rawJson = await pyodide.runPythonAsync(\`
import copy, json, sys

class _BlockedBrowserBridgeFinder:
    _BLOCKED = {"js", "pyodide", "pyodide_js", "pyodide_http"}
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in self._BLOCKED:
            raise ImportError(f"blocked_browser_bridge:{fullname}")
        return None

def _values_equal(left, right):
    if type(left) is not type(right):
        return False
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _values_equal(a, b) for a, b in zip(left, right)
        )
    if isinstance(left, dict):
        return set(left.keys()) == set(right.keys()) and all(
            _values_equal(left[k], right[k]) for k in left
        )
    return left == right

_ns = {"__name__": "client_sample_runner"}
_out = []
_finder = _BlockedBrowserBridgeFinder()
_saved_modules = {m: sys.modules.pop(m, None) for m in ("js", "pyodide_js", "pyodide_http")}
sys.meta_path.insert(0, _finder)
try:
    _comp = compile(_USER_CODE, "<student_submission>", "exec")
    exec(_comp, _ns, _ns)
    _fn = _ns.get(_FUNC_NAME)
    if not callable(_fn):
        raise TypeError("required_function_not_found")
    for _t in json.loads(_TESTS_JSON):
        try:
            _raw_val = _fn(*copy.deepcopy(_t["args"]))
            _val = json.loads(json.dumps(_raw_val, ensure_ascii=False, allow_nan=False))
            _ok = _values_equal(_val, _t["expected"])
            _out.append({
                "test_index": _t["test_index"],
                "status": "passed" if _ok else "wrong_answer",
                "args": _t["args"],
                "expected": _t["expected"],
                "actual": _val,
                "error_summary": None,
            })
        except Exception as _e:
            _out.append({
                "test_index": _t["test_index"],
                "status": "runtime_error",
                "args": _t["args"],
                "expected": _t["expected"],
                "actual": None,
                "error_summary": f"{type(_e).__name__}: {_e}",
            })
except SyntaxError as _se:
    for _t in json.loads(_TESTS_JSON):
        _out.append({
            "test_index": _t["test_index"],
            "status": "syntax_error",
            "args": _t["args"],
            "expected": _t["expected"],
            "actual": None,
            "error_summary": f"SyntaxError: {_se}",
        })
except Exception as _re:
    for _t in json.loads(_TESTS_JSON):
        _out.append({
            "test_index": _t["test_index"],
            "status": "runtime_error",
            "args": _t["args"],
            "expected": _t["expected"],
            "actual": None,
            "error_summary": f"{type(_re).__name__}: {_re}",
        })
finally:
    if _finder in sys.meta_path:
        sys.meta_path.remove(_finder)
    for _m, _mod in _saved_modules.items():
        if _mod is not None and _m not in sys.modules:
            sys.modules[_m] = _mod
json.dumps(_out, ensure_ascii=False)
          \`);
          self.postMessage({ type: "done", ok: true, results: JSON.parse(rawJson) });
        } catch (err) {
          self.postMessage({ type: "done", ok: false, error: err ? err.message : "Lỗi thực thi Pyodide Worker." });
        }
      };
    `;
    const blob = new Blob([workerSource], { type: "application/javascript" });
    const blobUrl = URL.createObjectURL(blob);
    const worker = new Worker(blobUrl);
    URL.revokeObjectURL(blobUrl);
    return worker;
  }

  function terminateWorker() {
    if (sampleWorker) {
      sampleWorker.terminate();
      sampleWorker = null;
    }
    activeRunId = null;
  }

  window.addEventListener("message", (event) => {
    if (event.source !== window.parent) return;
    const data = event.data || {};
    if (data.type === "terminate") {
      terminateWorker();
      return;
    }
    if (data.type !== "run" || !data.runId) return;

    activeRunId = data.runId;
    const runId = activeRunId;
    if (!sampleWorker) {
      sampleWorker = createPyodideWorker();
    }

    sampleWorker.onmessage = (workerEvent) => {
      if (activeRunId !== runId) return;
      const msg = workerEvent.data || {};
      if (msg.type === "ready") {
        window.parent.postMessage({ runId, type: "ready" }, "*");
        return;
      }
      window.parent.postMessage(
        {
          runId,
          type: "done",
          ok: Boolean(msg.ok),
          results: msg.results,
          error: msg.error,
        },
        "*"
      );
    };

    sampleWorker.onerror = (err) => {
      terminateWorker();
      window.parent.postMessage(
        {
          runId,
          type: "done",
          ok: false,
          error: err && err.message ? err.message : "Lỗi khởi tạo Pyodide Web Worker.",
        },
        "*"
      );
    };

    sampleWorker.postMessage({
      sourceCode: data.sourceCode,
      functionName: data.functionName,
      examples: data.examples,
    });
  });
})();
