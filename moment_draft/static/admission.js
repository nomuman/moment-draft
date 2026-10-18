const form = document.querySelector("#admission");
const code = document.querySelector("#code");
const button = form.querySelector("button");
const status = document.querySelector("#status");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  button.disabled = true;
  button.textContent = "確認しています…";
  status.textContent = "";
  code.removeAttribute("aria-invalid");
  try {
    const response = await fetch("/api/admission", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: code.value }),
      credentials: "same-origin",
    });
    const result = await response.json();
    if (!response.ok) {
      const retry = response.headers.get("Retry-After");
      status.textContent = retry ? `${retry}秒ほど待ってから、もう一度お試しください。` : result.error;
      code.setAttribute("aria-invalid", "true");
      code.focus();
      return;
    }
    code.value = "";
    location.replace("/");
  } catch {
    status.textContent = "接続を確認できませんでした。通信を確認して、もう一度お試しください。";
  } finally {
    button.disabled = false;
    button.textContent = "試用をはじめる";
  }
});
