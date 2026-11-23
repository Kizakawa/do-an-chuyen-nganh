(() => {
  function readCookie(name) {
    const prefix = `${encodeURIComponent(name)}=`;
    const item = document.cookie.split("; ").find((part) => part.startsWith(prefix));
    return item ? decodeURIComponent(item.slice(prefix.length)) : "";
  }

  window.pythonPracticeCsrfHeaders = () => {
    const meta = document.querySelector('meta[name="csrf-cookie-name"]');
    const token = readCookie(meta ? meta.content : "python_practice_csrf");
    return token ? {"X-CSRF-Token": token} : {};
  };

  const logout = document.querySelector("#logout");
  if (logout) {
    logout.addEventListener("click", async () => {
      logout.disabled = true;
      const response = await fetch("/api/auth/logout", {
        method: "POST",
        headers: window.pythonPracticeCsrfHeaders(),
      });
      window.location.assign(response.ok ? "/login" : "/ui/problems");
    });
  }
})();
