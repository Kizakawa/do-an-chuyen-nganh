(() => {
  const panel = document.querySelector(".editor-panel");
  if (!panel) return;

  const problemId = Number(panel.dataset.problemId);
  const problemSlug = panel.dataset.problemSlug;
  const functionName = panel.dataset.functionName;
  const source = document.querySelector("#source-code");
  const lineNumbers = document.querySelector("#line-numbers");
  const runSampleButton = document.querySelector("#run-public-samples");
  const submitButton = document.querySelector("#submit-code");
  const message = document.querySelector("#submit-message");
  const state = document.querySelector("#submission-state");
  const resultBox = document.querySelector("#grading-result");
  const summary = document.querySelector("#result-summary");
  const publicResults = document.querySelector("#public-results");

  const quotaBadge = document.querySelector("#quota-badge");
  const quotaDetail = document.querySelector("#quota-detail");
  const hintButton = document.querySelector("#request-hint");
  const cooldownTimer = document.querySelector("#cooldown-timer");
  const hintMessage = document.querySelector("#hint-message");
  const hintList = document.querySelector("#hint-list");

  const PYODIDE_INIT_TIMEOUT_MS = 60000;
  const CLIENT_SAMPLE_TIMEOUT_MS = 5000;

  const ERROR_MESSAGES = {
    authentication_required: "Vui lòng đăng nhập để tiếp tục.",
    invalid_or_expired_session: "Phiên đăng nhập đã hết hạn hoặc không hợp lệ. Vui lòng tải lại trang để đăng nhập.",
    csrf_validation_failed: "Phiên bảo mật CSRF không khớp. Vui lòng tải lại trang rồi thử lại.",
    cooldown_active: "Bạn vừa nhận gợi ý. Vui lòng chờ hết thời gian cooldown trước khi yêu cầu tiếp.",
    quota_exhausted: "Bạn đã sử dụng hết 3/3 lượt gợi ý cho bài tập này.",
    request_already_processing: "Hệ thống đang xử lý một yêu cầu gợi ý trước đó của bạn.",
    request_expired: "Yêu cầu gợi ý đã quá thời gian chờ. Bạn không bị trừ lượt, vui lòng thử lại.",
    worker_max_attempts_exceeded: "Worker xử lý gợi ý vượt quá số lần thử lại. Bạn không bị trừ lượt.",
    llm_worker_error: "Worker AI gặp sự cố tạm thời khi tạo gợi ý. Bạn không bị trừ lượt.",
    no_public_evidence_available: "Bài làm chỉ sai ở kiểm thử ẩn nên hệ thống không trích xuất dữ kiện để tránh lộ test ẩn.",
    all_passed: "Bài làm đã đạt toàn bộ kiểm thử nên không cần gợi ý sửa lỗi.",
    grading_pending: "Lượt chấm chưa hoàn tất. Vui lòng đợi kết quả chấm trước khi yêu cầu gợi ý.",
    grading_technical_error: "Lượt chấm gặp lỗi kỹ thuật nên chưa thể tạo gợi ý.",
    llm_busy: "Hệ thống trợ giảng AI đang xử lý tối đa luồng đồng thời. Vui lòng thử lại sau vài giây (bạn không bị trừ lượt).",
    hint_failure_rate_limited: "Bạn đã gặp lỗi tạo gợi ý nhiều lần liên tiếp. Vui lòng chờ một lát rồi thử lại (bạn không bị trừ lượt).",
    too_many_pending_submissions: "Bạn đang có quá nhiều bài nộp chờ chấm. Vui lòng đợi kết quả chấm xong rồi nộp tiếp.",
    login_rate_limited: "Đăng nhập sai quá số lần quy định. Vui lòng thử lại sau.",
  };

  let activeSubmissionId = null;
  let activeRunId = null;
  let activeRunEligibleForHint = false;
  let isRequestingHint = false;
  let sandboxFrame = null;
  let sandboxLoadPromise = null;
  let cancelActiveSampleRun = null;
  let pollTimerId = null;
  let currentQuota = {
    used_count: 0,
    max_quota: 3,
    remaining_count: 3,
    next_level: "KM",
    cooldown_remaining_seconds: 0,
    has_processing_request: false,
  };
  let cooldownIntervalId = null;

  function syncLineNumbers() {
    if (!source || !lineNumbers) return;
    const count = Math.max(1, source.value.split("\n").length);
    const nums = [];
    for (let i = 1; i <= count; i += 1) {
      nums.push(i);
    }
    lineNumbers.textContent = nums.join("\n");
    lineNumbers.scrollTop = source.scrollTop;
  }

  function setState(text, kind = "neutral") {
    state.textContent = text;
    state.className = `status-pill ${kind}`;
  }

  function makeRequestKey() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID();
    }
    return `req-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function formatApiError(code, fallback) {
    if (!code) return fallback;
    return ERROR_MESSAGES[code] || code;
  }

  function formatHintError(code) {
    return formatApiError(code, "Không thể yêu cầu gợi ý.");
  }

  function updateHintButtonState() {
    if (!hintButton) return;
    const canRequest =
      !isRequestingHint &&
      activeRunEligibleForHint &&
      currentQuota.remaining_count > 0 &&
      currentQuota.cooldown_remaining_seconds <= 0 &&
      !currentQuota.has_processing_request;
    hintButton.disabled = !canRequest;
    if (currentQuota.remaining_count <= 0) {
      hintButton.textContent = "Đã hết lượt gợi ý";
    } else if (currentQuota.next_level) {
      hintButton.textContent = `Yêu cầu gợi ý (${currentQuota.next_level})`;
    } else {
      hintButton.textContent = "Yêu cầu gợi ý";
    }
  }

  function startCooldownCountdown(seconds) {
    if (cooldownIntervalId) {
      window.clearInterval(cooldownIntervalId);
      cooldownIntervalId = null;
    }
    currentQuota.cooldown_remaining_seconds = Math.max(0, Number(seconds) || 0);
    if (!cooldownTimer) return;

    const tick = () => {
      if (currentQuota.cooldown_remaining_seconds <= 0) {
        cooldownTimer.hidden = true;
        cooldownTimer.textContent = "";
        if (cooldownIntervalId) {
          window.clearInterval(cooldownIntervalId);
          cooldownIntervalId = null;
        }
        updateHintButtonState();
        return;
      }
      cooldownTimer.hidden = false;
      cooldownTimer.textContent = `Chờ cooldown: ${currentQuota.cooldown_remaining_seconds}s`;
      currentQuota.cooldown_remaining_seconds -= 1;
      updateHintButtonState();
    };

    tick();
    if (currentQuota.cooldown_remaining_seconds > 0) {
      cooldownIntervalId = window.setInterval(tick, 1000);
    }
  }

  function renderQuota(quota) {
    if (!quota) return;
    currentQuota = quota;
    if (quotaBadge) {
      quotaBadge.textContent = `Còn ${quota.remaining_count}/${quota.max_quota} lượt`;
      quotaBadge.className = `status-pill ${quota.remaining_count > 0 ? "neutral" : "danger"}`;
    }
    if (quotaDetail) {
      if (quota.remaining_count <= 0) {
        quotaDetail.textContent = "Bạn đã sử dụng hết 3/3 lượt gợi ý cho bài tập này.";
      } else {
        const levelDesc =
          quota.next_level === "KM"
            ? "KM (Nhận diện vị trí và nguyên nhân lỗi)"
            : "KH (Định hướng bước kiểm tra và sửa bài)";
        quotaDetail.textContent = `Mức gợi ý tiếp theo: ${levelDesc}. Đã dùng ${quota.used_count}/${quota.max_quota} lượt.`;
      }
    }
    startCooldownCountdown(quota.cooldown_remaining_seconds);
    updateHintButtonState();
  }

  function createHintCard(item) {
    const card = document.createElement("article");
    const levelClass = (item.level || "KM").toLowerCase();
    card.className = `hint-card level-${levelClass}`;

    const header = document.createElement("div");
    header.className = "hint-card-header";
    const strong = document.createElement("strong");
    strong.textContent = `Gợi ý mức ${item.level}`;
    const meta = document.createElement("span");
    meta.textContent = `Lượt chấm #${item.grading_run_id} · ${item.model_id}`;
    header.append(strong, meta);

    const body = document.createElement("p");
    body.className = "hint-body";
    body.textContent = item.hint_text || "";

    card.append(header, body);
    return card;
  }

  async function pollHintRequest(requestId, attempts = 0) {
    if (attempts >= 120) throw new Error("Yêu cầu gợi ý chờ quá lâu. Vui lòng thử lại sau.");
    const response = await fetch(`/api/hint-requests/${requestId}`);
    if (!response.ok) throw new Error("Không đọc được trạng thái gợi ý.");
    const payload = await response.json();
    if (payload.status === "processing") {
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
      return pollHintRequest(requestId, attempts + 1);
    }
    return payload;
  }

  function renderHintList(hints) {
    if (!hintList) return;
    hintList.replaceChildren();
    (hints || []).forEach((item) => {
      hintList.append(createHintCard(item));
    });
  }

  async function loadProblemHints() {
    if (!problemSlug) return;
    try {
      const response = await fetch(`/api/problems/${problemSlug}/hints`);
      if (!response.ok) return;
      const payload = await response.json();
      renderQuota(payload.quota);
      renderHintList(payload.hints);
    } catch (_) {
      // Bỏ qua lỗi mạng tạm thời khi tải trạng thái ban đầu
    }
  }

  function renderTestCards(results) {
    publicResults.replaceChildren();
    results.forEach((item) => {
      const card = document.createElement("article");
      card.className = `test-result ${item.status === "passed" ? "passed" : "failed"}`;
      const heading = document.createElement("strong");
      heading.textContent = `Test ${item.test_index}: ${item.status}`;
      const details = document.createElement("pre");
      details.textContent = `args = ${JSON.stringify(item.args)}\nexpected = ${JSON.stringify(item.expected)}\nactual = ${JSON.stringify(item.actual)}`;
      card.append(heading, details);
      if (item.error_summary) {
        const error = document.createElement("p");
        error.textContent = item.error_summary;
        card.append(error);
      }
      publicResults.append(card);
    });
  }

  function renderResult(payload) {
    const totals = payload.summary;
    resultBox.hidden = false;
    summary.textContent = `${totals.passed_count}/${totals.expected_test_count} test đạt.`;
    renderTestCards(payload.public_results);

    if (payload.status === "completed") {
      setState(payload.all_passed ? "Đạt" : "Chưa đạt", payload.all_passed ? "success" : "danger");
      activeSubmissionId = payload.submission_id;
      activeRunId = payload.grading_run_id;
      activeRunEligibleForHint = payload.all_passed === false;
    } else if (payload.status === "technical_error") {
      setState("Lỗi kỹ thuật", "danger");
      message.textContent = "Không thể kết luận bài làm. Hãy thử lại sau.";
      activeRunEligibleForHint = false;
    }
    updateHintButtonState();
  }

  async function pollResult(submissionId, runId, attempts = 0) {
    if (attempts >= 120) {
      setState("Chờ quá lâu", "danger");
      message.textContent = "Lượt chấm chưa hoàn tất. Bạn có thể xem lại trong lịch sử.";
      submitButton.disabled = false;
      return;
    }
    try {
      const response = await fetch(`/api/submissions/${submissionId}/grading-runs/${runId}`);
      if (!response.ok) throw new Error("Không đọc được kết quả chấm.");
      const payload = await response.json();
      if (payload.status === "queued" || payload.status === "running") {
        setState(payload.status === "queued" ? "Đang chờ" : "Đang chấm");
        pollTimerId = window.setTimeout(() => pollResult(submissionId, runId, attempts + 1), 1000);
        return;
      }
      renderResult(payload);
      submitButton.disabled = false;
    } catch (error) {
      setState("Lỗi kết nối", "danger");
      message.textContent = error.message;
      submitButton.disabled = false;
    }
  }

  function destroySandboxFrame() {
    if (sandboxFrame) {
      try {
        if (sandboxFrame.contentWindow) {
          sandboxFrame.contentWindow.postMessage({ type: "terminate" }, "*");
        }
      } catch (_) {}
      sandboxFrame.remove();
      sandboxFrame = null;
    }
    sandboxLoadPromise = null;
  }

  function ensureSandboxFrame() {
    if (sandboxFrame && sandboxFrame.isConnected && sandboxLoadPromise) {
      return sandboxLoadPromise;
    }
    destroySandboxFrame();
    const frame = document.createElement("iframe");
    frame.setAttribute("sandbox", "allow-scripts");
    frame.src = "/static/pyodide-sandbox.html";
    frame.hidden = true;
    frame.setAttribute("aria-hidden", "true");
    sandboxFrame = frame;
    sandboxLoadPromise = new Promise((resolve, reject) => {
      frame.addEventListener("load", () => resolve(frame), { once: true });
      frame.addEventListener(
        "error",
        () => reject(new Error("Không thể khởi tạo Pyodide sandbox iframe.")),
        { once: true }
      );
    });
    document.body.append(frame);
    return sandboxLoadPromise;
  }

  function runSamplesInWebWorker(sourceCode, fnName, examples) {
    return new Promise((resolve, reject) => {
      const runId = makeRequestKey();
      let settled = false;
      let execTimeoutId = null;
      let initTimeoutId = null;

      const clearTimers = () => {
        if (initTimeoutId) {
          window.clearTimeout(initTimeoutId);
          initTimeoutId = null;
        }
        if (execTimeoutId) {
          window.clearTimeout(execTimeoutId);
          execTimeoutId = null;
        }
      };

      const cleanup = () => {
        clearTimers();
        window.removeEventListener("message", onMessage);
        if (cancelActiveSampleRun) {
          cancelActiveSampleRun = null;
        }
      };

      const onMessage = (event) => {
        if (!sandboxFrame || event.source !== sandboxFrame.contentWindow) return;
        const data = event.data || {};
        if (data.runId !== runId) return;

        if (data.type === "ready") {
          if (initTimeoutId) {
            window.clearTimeout(initTimeoutId);
            initTimeoutId = null;
          }
          message.textContent = "Đang thực thi 3 test công khai trong Web Worker cô lập (giới hạn 5s)...";
          execTimeoutId = window.setTimeout(() => {
            if (settled) return;
            settled = true;
            cleanup();
            destroySandboxFrame();
            const tleResults = examples.map((t) => ({
              test_index: t.test_index,
              status: "time_limit_exceeded",
              args: t.args,
              expected: t.expected,
              actual: null,
              error_summary: "Vòng lặp quá 5 giây: Web Worker đã tự động ngắt để bảo vệ trình duyệt.",
            }));
            resolve(tleResults);
          }, CLIENT_SAMPLE_TIMEOUT_MS);
          return;
        }

        if (data.type === "done") {
          if (settled) return;
          settled = true;
          cleanup();
          if (data.ok) {
            resolve(data.results);
          } else {
            reject(new Error(data.error || "Lỗi chạy thử trên Web Worker."));
          }
        }
      };

      initTimeoutId = window.setTimeout(() => {
        if (settled) return;
        settled = true;
        cleanup();
        destroySandboxFrame();
        reject(new Error("Quá thời gian tải thư viện Pyodide từ CDN. Hãy kiểm tra kết nối mạng."));
      }, PYODIDE_INIT_TIMEOUT_MS);

      cancelActiveSampleRun = () => {
        if (settled) return;
        settled = true;
        cleanup();
        destroySandboxFrame();
        reject(new Error("Đã dừng chạy thử theo yêu cầu của bạn."));
      };

      window.addEventListener("message", onMessage);
      ensureSandboxFrame()
        .then((frame) => {
          if (settled || !frame.contentWindow) return;
          frame.contentWindow.postMessage(
            {
              runId,
              type: "run",
              sourceCode,
              functionName: fnName,
              examples,
            },
            "*"
          );
        })
        .catch((err) => {
          if (settled) return;
          settled = true;
          cleanup();
          destroySandboxFrame();
          reject(err);
        });
    });
  }

  if (runSampleButton && functionName) {
    runSampleButton.addEventListener("click", async () => {
      if (cancelActiveSampleRun) {
        cancelActiveSampleRun();
        return;
      }
      runSampleButton.textContent = "Dừng chạy thử";
      runSampleButton.classList.remove("secondary");
      runSampleButton.classList.add("danger");
      message.textContent = "Đang khởi tạo môi trường Pyodide Web Worker...";
      try {
        const examples = Array.from(document.querySelectorAll(".examples .example")).map((el) => ({
          test_index: Number(el.dataset.testIndex),
          args: JSON.parse(el.dataset.args || "[]"),
          expected: JSON.parse(el.dataset.expected || "null"),
        }));
        const results = await runSamplesInWebWorker(source.value, functionName, examples);
        const passed = results.filter((r) => r.status === "passed").length;
        resultBox.hidden = false;
        summary.textContent = `[Chạy thử Client Web Worker] ${passed}/${results.length} ví dụ công khai đạt.`;
        renderTestCards(results);
        setState(passed === results.length ? "Ví dụ đạt" : "Ví dụ chưa đạt", passed === results.length ? "success" : "danger");
        message.textContent = "Đã chạy thử trong Web Worker. Bấm 'Nộp bài' để chấm chính thức 8 test trên Judge0.";
      } catch (err) {
        message.textContent = err.message || "Lỗi khi chạy thử trên trình duyệt.";
      } finally {
        runSampleButton.textContent = "Chạy thử ví dụ (Client)";
        runSampleButton.classList.remove("danger");
        runSampleButton.classList.add("secondary");
      }
    });
  }

  if (source) {
    syncLineNumbers();
    source.addEventListener("input", syncLineNumbers);
    source.addEventListener("scroll", () => {
      if (lineNumbers) lineNumbers.scrollTop = source.scrollTop;
    });
    source.addEventListener("keydown", (event) => {
      if (event.key === "Tab") {
        event.preventDefault();
        const start = source.selectionStart;
        const end = source.selectionEnd;
        source.value = `${source.value.slice(0, start)}    ${source.value.slice(end)}`;
        source.selectionStart = source.selectionEnd = start + 4;
        syncLineNumbers();
      }
    });
  }

  submitButton.addEventListener("click", async () => {
    if (pollTimerId) {
      window.clearTimeout(pollTimerId);
      pollTimerId = null;
    }
    submitButton.disabled = true;
    activeSubmissionId = null;
    activeRunId = null;
    activeRunEligibleForHint = false;
    updateHintButtonState();
    message.textContent = "";
    if (hintMessage) hintMessage.textContent = "";
    resultBox.hidden = true;
    setState("Đang gửi");
    try {
      const response = await fetch("/api/submissions", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(window.pythonPracticeCsrfHeaders ? window.pythonPracticeCsrfHeaders() : {}),
        },
        body: JSON.stringify({problem_id: problemId, source_code: source.value}),
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(formatApiError(payload.detail, "Không thể nộp bài."));
      }
      setState("Đang chờ");
      pollResult(payload.submission_id, payload.grading_run_id);
    } catch (error) {
      setState("Nộp thất bại", "danger");
      message.textContent = error.message;
      submitButton.disabled = false;
    }
  });

  if (hintButton) {
    hintButton.addEventListener("click", async () => {
      if (!activeSubmissionId || !activeRunId || isRequestingHint) return;
      isRequestingHint = true;
      updateHintButtonState();
      if (hintMessage) {
        hintMessage.className = "hint-status-msg";
        hintMessage.textContent = "Đang tạo và kiểm duyệt gợi ý...";
      }
      try {
        const response = await fetch(`/api/submissions/${activeSubmissionId}/hints`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            ...(window.pythonPracticeCsrfHeaders ? window.pythonPracticeCsrfHeaders() : {}),
          },
          body: JSON.stringify({
            grading_run_id: activeRunId,
            request_key: makeRequestKey(),
          }),
        });
        let payload = await response.json();
        if (!response.ok) {
          if (typeof payload.remaining_count === "number") {
            currentQuota.remaining_count = payload.remaining_count;
            currentQuota.used_count = Math.max(0, currentQuota.max_quota - payload.remaining_count);
          }
          if (payload.retry_after_seconds) {
            startCooldownCountdown(payload.retry_after_seconds);
          }
          throw new Error(formatHintError(payload.error_code || payload.detail));
        }
        if (payload.status === "processing") {
          if (hintMessage) hintMessage.textContent = "Gợi ý đang chờ worker AI xử lý...";
          payload = await pollHintRequest(payload.request_id);
        }
        if (payload.quota) {
          renderQuota(payload.quota);
        }
        if (payload.status === "succeeded" && payload.hint_text) {
          if (hintMessage) {
            hintMessage.className = "hint-status-msg success";
            hintMessage.textContent = `Đã lưu gợi ý mức ${payload.level}.`;
          }
          if (hintList) {
            hintList.append(createHintCard(payload));
          }
        } else {
          if (hintMessage) {
            hintMessage.className = "hint-status-msg danger";
            hintMessage.textContent = `Không tạo được gợi ý hợp lệ (${formatHintError(payload.error_code)}) — bạn không bị trừ lượt.`;
          }
        }
      } catch (error) {
        if (hintMessage) {
          hintMessage.className = "hint-status-msg danger";
          hintMessage.textContent = error.message;
        }
      } finally {
        isRequestingHint = false;
        updateHintButtonState();
      }
    });
  }

  loadProblemHints();
})();
