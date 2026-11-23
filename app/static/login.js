(() => {
  const form = document.querySelector("#login-form");
  if (!form) return;
  const errorBox = document.querySelector("#login-error");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    errorBox.textContent = "";
    const button = form.querySelector("button");
    button.disabled = true;
    try {
      const response = await fetch("/api/auth/login", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          login_name: form.elements.login_name.value,
          password: form.elements.password.value,
        }),
      });
      if (!response.ok) {
        if (response.status === 429) {
          throw new Error("Đăng nhập sai quá số lần quy định. Vui lòng đợi vài phút rồi thử lại.");
        }
        throw new Error("Tên đăng nhập hoặc mật khẩu không đúng.");
      }
      window.location.replace("/ui/problems");
    } catch (error) {
      errorBox.textContent = error.message;
      button.disabled = false;
    }
  });
})();
