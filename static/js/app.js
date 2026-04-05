import { clearToken, getMe } from "./api.js";
import { initAdminPage } from "./page-admin.js";
import { initCharactersPage } from "./page-characters.js";
import { initHistoryPage } from "./page-history.js";
import { initLibraryPage } from "./page-library.js";
import { initLoginPage } from "./page-login.js";
import { initSettingsPage } from "./page-settings.js";
import { initWorldPage } from "./page-world.js";
import { initWriterPage } from "./page-writer.js";
import {
  bindCommonActions,
  initGlobalAssistant,
  renderShell,
  showToast,
  updateServiceStatus,
} from "./shell.js";

async function initProtectedPage(page) {
  let user;
  try {
    user = await getMe();
  } catch {
    clearToken();
    window.location.href = "/login";
    return;
  }

  const template = document.getElementById("page-template");
  const content = template ? template.innerHTML : "";
  document.body.innerHTML = renderShell(page, content, user);
  bindCommonActions();
  await updateServiceStatus();

  if (page === "library") await initLibraryPage(user);
  else if (page === "characters") await initCharactersPage(user);
  else if (page === "writer") await initWriterPage(user);
  else if (page === "world") await initWorldPage(user);
  else if (page === "settings") await initSettingsPage(user);
  else if (page === "history") await initHistoryPage(user);
  else if (page === "admin") await initAdminPage(user);
  else showToast("该页面后续再补齐。");

  await initGlobalAssistant(page);
}

document.addEventListener("DOMContentLoaded", async () => {
  const page = document.body.dataset.page;
  if (page === "login") {
    await initLoginPage();
    return;
  }

  if (document.body.dataset.protected === "true") {
    await initProtectedPage(page);
  }
});
