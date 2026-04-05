import { ApiError, clearToken, defaultRouteForUser, getMe, getToken, login } from "./api.js";
import { updateServiceStatus } from "./shell.js";

export async function initLoginPage() {
  await updateServiceStatus();

  if (getToken()) {
    try {
      const user = await getMe();
      window.location.href = defaultRouteForUser(user);
      return;
    } catch {
      clearToken();
    }
  }

  const form = document.getElementById("login-form");
  const submitButton = document.getElementById("login-submit");
  const errorBox = document.getElementById("login-error");
  const passwordField = document.getElementById("password");
  const togglePassword = document.getElementById("toggle-password");

  if (togglePassword && passwordField) {
    togglePassword.addEventListener("click", () => {
      const isPassword = passwordField.type === "password";
      passwordField.type = isPassword ? "text" : "password";
      togglePassword.textContent = isPassword ? "隐藏" : "显示";
    });
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    errorBox.classList.add("hidden");
    submitButton.disabled = true;
    submitButton.textContent = "登录中...";

    try {
      const username = document.getElementById("username").value.trim();
      const password = document.getElementById("password").value;
      const response = await login(username, password);
      window.location.href = defaultRouteForUser(response.user);
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "登录失败，请稍后重试。";
      errorBox.textContent = message;
      errorBox.classList.remove("hidden");
    } finally {
      submitButton.disabled = false;
      submitButton.textContent = "进入竹林工作台";
    }
  });
}
