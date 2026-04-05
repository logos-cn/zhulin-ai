const TOKEN_KEY = "bamboo_ai_access_token";

export class ApiError extends Error {
  constructor(message, status, data = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.data = data;
  }
}

function buildQuery(params = {}) {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value === undefined || value === null || value === "") return;
    query.set(key, String(value));
  });
  const raw = query.toString();
  return raw ? `?${raw}` : "";
}

async function parseBody(response) {
  if (response.status === 204 || response.status === 205) return null;
  const contentType = response.headers.get("content-type") || "";
  const contentLength = response.headers.get("content-length");
  if (contentLength === "0") return null;
  if (contentType.includes("application/json")) {
    const raw = await response.text();
    if (!raw.trim()) return null;
    return JSON.parse(raw);
  }
  return response.text();
}

export function getToken() {
  const sessionToken = window.sessionStorage.getItem(TOKEN_KEY);
  if (sessionToken) return sessionToken;

  const legacyToken = window.localStorage.getItem(TOKEN_KEY);
  if (!legacyToken) return null;

  window.sessionStorage.setItem(TOKEN_KEY, legacyToken);
  window.localStorage.removeItem(TOKEN_KEY);
  return legacyToken;
}

export function setToken(token) {
  window.sessionStorage.setItem(TOKEN_KEY, token);
  window.localStorage.removeItem(TOKEN_KEY);
}

export function clearToken() {
  window.sessionStorage.removeItem(TOKEN_KEY);
  window.localStorage.removeItem(TOKEN_KEY);
}

export function isAdminRole(role) {
  return role === "super_admin" || role === "admin";
}

export function defaultRouteForUser() {
  return "/library";
}

export async function apiFetch(path, options = {}) {
  const { auth = true, headers = {}, body, ...rest } = options;
  const isFormData = typeof FormData !== "undefined" && body instanceof FormData;
  const finalHeaders = {
    ...(body && !isFormData ? { "Content-Type": "application/json" } : {}),
    ...headers,
  };

  const token = getToken();
  if (auth && token) {
    finalHeaders.Authorization = `Bearer ${token}`;
  }

  const response = await fetch(path, {
    ...rest,
    headers: finalHeaders,
    body: body ? (isFormData ? body : JSON.stringify(body)) : undefined,
  });

  const data = await parseBody(response);
  if (!response.ok) {
    const message =
      typeof data === "object" && data && "detail" in data ? data.detail : response.statusText;
    throw new ApiError(message || "请求失败。", response.status, data);
  }

  return data;
}

export async function apiFetchBlob(path, options = {}) {
  const { auth = true, headers = {}, body, ...rest } = options;
  const isFormData = typeof FormData !== "undefined" && body instanceof FormData;
  const finalHeaders = {
    ...headers,
  };

  const token = getToken();
  if (auth && token) {
    finalHeaders.Authorization = `Bearer ${token}`;
  }

  const response = await fetch(path, {
    ...rest,
    headers: finalHeaders,
    body: body ? (isFormData ? body : JSON.stringify(body)) : undefined,
  });

  if (!response.ok) {
    const data = await parseBody(response);
    const message =
      typeof data === "object" && data && "detail" in data ? data.detail : response.statusText;
    throw new ApiError(message || "请求失败。", response.status, data);
  }

  return response.blob();
}

export async function login(username, password) {
  const data = await apiFetch("/api/v1/auth/login", {
    method: "POST",
    auth: false,
    body: { username, password },
  });
  setToken(data.access_token);
  return data;
}

export function logout() {
  clearToken();
  window.location.href = "/login";
}

export function getMe() {
  return apiFetch("/api/v1/auth/me");
}

export function getHealth() {
  return apiFetch("/healthz", { auth: false });
}

export function listBooks() {
  return apiFetch("/api/v1/books");
}

export function getBook(bookId) {
  return apiFetch(`/api/v1/books/${bookId}`);
}

export function createBook(payload) {
  return apiFetch("/api/v1/books", { method: "POST", body: payload });
}

export function updateBook(bookId, payload) {
  return apiFetch(`/api/v1/books/${bookId}`, { method: "PUT", body: payload });
}

export function exportBookProjectArchive(bookId) {
  return apiFetchBlob(`/api/v1/books/${bookId}/project-archive`);
}

export function importBookProjectArchive(bookId, file, payload = {}) {
  const formData = new FormData();
  formData.append("file", file);
  Object.entries(payload).forEach(([key, value]) => {
    if (value === undefined || value === null || value === "") return;
    formData.append(key, String(value));
  });
  return apiFetch(`/api/v1/books/${bookId}/project-archive/import`, {
    method: "POST",
    body: formData,
  });
}

export function previewBookProjectArchive(bookId, file) {
  const formData = new FormData();
  formData.append("file", file);
  return apiFetch(`/api/v1/books/${bookId}/project-archive/preview`, {
    method: "POST",
    body: formData,
  });
}

export function applyBookProjectArchiveImport(bookId, payload) {
  return apiFetch(`/api/v1/books/${bookId}/project-archive/apply`, {
    method: "POST",
    body: payload,
  });
}

export function deleteBook(bookId) {
  return apiFetch(`/api/v1/books/${bookId}`, { method: "DELETE" });
}

export function listChapters(bookId) {
  return apiFetch(`/api/v1/books/${bookId}/chapters`);
}

export function getChapter(bookId, chapterId) {
  return apiFetch(`/api/v1/books/${bookId}/chapters/${chapterId}`);
}

export function createChapter(bookId, payload) {
  return apiFetch(`/api/v1/books/${bookId}/chapters`, { method: "POST", body: payload });
}

export function updateChapter(bookId, chapterId, payload) {
  return apiFetch(`/api/v1/books/${bookId}/chapters/${chapterId}`, {
    method: "PUT",
    body: payload,
  });
}

export function updateChapterKeepalive(bookId, chapterId, payload) {
  const headers = { "Content-Type": "application/json" };
  const token = getToken();
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }

  return fetch(`/api/v1/books/${bookId}/chapters/${chapterId}`, {
    method: "PUT",
    headers,
    body: JSON.stringify(payload),
    keepalive: true,
  });
}

export function deleteChapter(bookId, chapterId) {
  return apiFetch(`/api/v1/books/${bookId}/chapters/${chapterId}`, { method: "DELETE" });
}

export function listSnapshots(bookId, params = {}) {
  return apiFetch(`/api/v1/books/${bookId}/snapshots${buildQuery(params)}`);
}

export function getSnapshot(bookId, snapshotId) {
  return apiFetch(`/api/v1/books/${bookId}/snapshots/${snapshotId}`);
}

export function listRecentSnapshots(limit = 8) {
  return apiFetch(`/api/v1/snapshots/recent${buildQuery({ limit })}`);
}

export function previewAI(bookId, chapterId, payload) {
  return apiFetch(`/api/v1/books/${bookId}/chapters/${chapterId}/ai/context-preview`, {
    method: "POST",
    body: payload,
  });
}

export function generateAIDraft(bookId, chapterId, payload) {
  return apiFetch(`/api/v1/books/${bookId}/chapters/${chapterId}/ai/generate-draft`, {
    method: "POST",
    body: payload,
  });
}

export async function streamAIDraft(bookId, chapterId, payload, handlers = {}) {
  const { onEvent } = handlers;
  const headers = { "Content-Type": "application/json" };
  const token = getToken();
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }

  const response = await fetch(`/api/v1/books/${bookId}/chapters/${chapterId}/ai/generate-draft-stream`, {
    method: "POST",
    headers,
    body: JSON.stringify(payload),
  });

  if (!response.ok) {
    const data = await parseBody(response);
    const message =
      typeof data === "object" && data && "detail" in data ? data.detail : response.statusText || "流式生成失败。";
    throw new ApiError(message, response.status, data);
  }

  if (!response.body) {
    throw new ApiError("当前浏览器没有返回可读取的流式响应。", response.status, null);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let finalResponse = null;

  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });

    let newlineIndex = buffer.indexOf("\n");
    while (newlineIndex >= 0) {
      const line = buffer.slice(0, newlineIndex).trim();
      buffer = buffer.slice(newlineIndex + 1);
      if (line) {
        const event = JSON.parse(line);
        if (typeof onEvent === "function") {
          onEvent(event);
        }
        if (event?.type === "error") {
          throw new ApiError(event.message || "流式生成失败。", response.status, event);
        }
        if (event?.type === "final" && event.response) {
          finalResponse = event.response;
        }
      }
      newlineIndex = buffer.indexOf("\n");
    }

    if (done) {
      break;
    }
  }

  if (buffer.trim()) {
    const event = JSON.parse(buffer.trim());
    if (typeof onEvent === "function") {
      onEvent(event);
    }
    if (event?.type === "error") {
      throw new ApiError(event.message || "流式生成失败。", response.status, event);
    }
    if (event?.type === "final" && event.response) {
      finalResponse = event.response;
    }
  }

  if (!finalResponse) {
    throw new ApiError("流式生成没有返回最终结果。", response.status, null);
  }
  return finalResponse;
}

export function refineAIDraft(bookId, chapterId, payload) {
  return apiFetch(`/api/v1/books/${bookId}/chapters/${chapterId}/ai/refine-draft`, {
    method: "POST",
    body: payload,
  });
}

export function applyAIDraft(bookId, chapterId, payload) {
  return apiFetch(`/api/v1/books/${bookId}/chapters/${chapterId}/ai/apply-draft`, {
    method: "POST",
    body: payload,
  });
}

export function extractWorld(bookId, chapterId, payload) {
  return apiFetch(`/api/v1/books/${bookId}/chapters/${chapterId}/ai/extract-world`, {
    method: "POST",
    body: payload,
  });
}

export function extractWorldBatch(bookId, payload) {
  return apiFetch(`/api/v1/books/${bookId}/ai/extract-world`, {
    method: "POST",
    body: payload,
  });
}

export function listWorldExtractionJobs(bookId, params = {}) {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value === undefined || value === null || value === "") return;
    query.set(key, String(value));
  });
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return apiFetch(`/api/v1/books/${bookId}/ai/extract-world/jobs${suffix}`);
}

export function getWorldExtractionJob(bookId, jobId) {
  return apiFetch(`/api/v1/books/${bookId}/ai/extract-world/jobs/${jobId}`);
}

export function cancelWorldExtractionJob(bookId, jobId) {
  return apiFetch(`/api/v1/books/${bookId}/ai/extract-world/jobs/${jobId}/cancel`, {
    method: "POST",
  });
}

export function resumeWorldExtractionJob(bookId, jobId, payload = {}) {
  return apiFetch(`/api/v1/books/${bookId}/ai/extract-world/jobs/${jobId}/resume`, {
    method: "POST",
    body: payload,
  });
}

export function deleteWorldExtractionJob(bookId, jobId) {
  return apiFetch(`/api/v1/books/${bookId}/ai/extract-world/jobs/${jobId}`, {
    method: "DELETE",
  });
}

export function resolveWorldExtractionConflict(bookId, jobId, payload) {
  return apiFetch(`/api/v1/books/${bookId}/ai/extract-world/jobs/${jobId}/resolve-conflict`, {
    method: "POST",
    body: payload,
  });
}

export function startWorldExtractionJob(bookId, payload) {
  return apiFetch(`/api/v1/books/${bookId}/ai/extract-world/jobs`, {
    method: "POST",
    body: payload,
  });
}

export function uploadWorldExtractionDocument(bookId, file, payload = {}) {
  const formData = new FormData();
  formData.append("file", file);
  Object.entries(payload).forEach(([key, value]) => {
    if (value === undefined || value === null || value === "") return;
    formData.append(key, String(value));
  });
  return apiFetch(`/api/v1/books/${bookId}/ai/extract-world/jobs/import`, {
    method: "POST",
    body: formData,
  });
}

export function estimateWorldExtractionDocument(bookId, file, payload = {}) {
  const formData = new FormData();
  formData.append("file", file);
  Object.entries(payload).forEach(([key, value]) => {
    if (value === undefined || value === null || value === "") return;
    formData.append(key, String(value));
  });
  return apiFetch(`/api/v1/books/${bookId}/ai/extract-world/import-estimate`, {
    method: "POST",
    body: formData,
  });
}

export function sendAssistantMessage(payload) {
  return apiFetch("/api/v1/assistant/chat", {
    method: "POST",
    body: payload,
  });
}

export async function streamAssistantMessage(payload, handlers = {}) {
  const { onEvent } = handlers;
  const headers = { "Content-Type": "application/json" };
  const token = getToken();
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }

  const response = await fetch("/api/v1/assistant/chat-stream", {
    method: "POST",
    headers,
    body: JSON.stringify(payload),
  });

  if (!response.ok) {
    const data = await parseBody(response);
    const message =
      typeof data === "object" && data && "detail" in data ? data.detail : response.statusText || "助手流式输出失败。";
    throw new ApiError(message, response.status, data);
  }

  if (!response.body) {
    throw new ApiError("当前浏览器没有返回可读取的助手流式响应。", response.status, null);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let finalResponse = null;

  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });

    let newlineIndex = buffer.indexOf("\n");
    while (newlineIndex >= 0) {
      const line = buffer.slice(0, newlineIndex).trim();
      buffer = buffer.slice(newlineIndex + 1);
      if (line) {
        const event = JSON.parse(line);
        if (typeof onEvent === "function") {
          onEvent(event);
        }
        if (event?.type === "error") {
          throw new ApiError(event.message || "助手流式输出失败。", response.status, event);
        }
        if (event?.type === "final" && event.response) {
          finalResponse = event.response;
        }
      }
      newlineIndex = buffer.indexOf("\n");
    }

    if (done) break;
  }

  if (buffer.trim()) {
    const event = JSON.parse(buffer.trim());
    if (typeof onEvent === "function") {
      onEvent(event);
    }
    if (event?.type === "error") {
      throw new ApiError(event.message || "助手流式输出失败。", response.status, event);
    }
    if (event?.type === "final" && event.response) {
      finalResponse = event.response;
    }
  }

  if (!finalResponse) {
    throw new ApiError("助手流式输出没有返回最终结果。", response.status, null);
  }
  return finalResponse;
}

export function listAIConfigs(params = {}) {
  return apiFetch(`/api/v1/ai-configs${buildQuery(params)}`);
}

export function discoverAIModels(payload) {
  return apiFetch("/api/v1/ai-configs/discover-models", {
    method: "POST",
    body: payload,
  });
}

export function testAIConnection(payload) {
  return apiFetch("/api/v1/ai-configs/test-connection", {
    method: "POST",
    body: payload,
  });
}

export function createAIConfig(payload) {
  return apiFetch("/api/v1/ai-configs", { method: "POST", body: payload });
}

export function updateAIConfig(configId, payload) {
  return apiFetch(`/api/v1/ai-configs/${configId}`, { method: "PUT", body: payload });
}

export function deleteAIConfig(configId) {
  return apiFetch(`/api/v1/ai-configs/${configId}`, { method: "DELETE" });
}

export function listUsers() {
  return apiFetch("/api/v1/users");
}

export function createUser(payload) {
  return apiFetch("/api/v1/users", { method: "POST", body: payload });
}

export function updateUser(userId, payload) {
  return apiFetch(`/api/v1/users/${userId}`, { method: "PUT", body: payload });
}

export function resetUserPassword(userId, payload) {
  return apiFetch(`/api/v1/users/${userId}/reset-password`, { method: "POST", body: payload });
}

export function getDatabaseBackupSettings() {
  return apiFetch("/api/v1/admin/database-backup");
}

export function updateDatabaseBackupSettings(payload) {
  return apiFetch("/api/v1/admin/database-backup", { method: "PUT", body: payload });
}

export function runDatabaseBackupNow() {
  return apiFetch("/api/v1/admin/database-backup/run", { method: "POST" });
}

export function downloadDatabaseBackup(filename) {
  return apiFetchBlob(`/api/v1/admin/database-backup/files/${encodeURIComponent(filename)}`);
}

export function restoreDatabaseBackup(payload) {
  return apiFetch("/api/v1/admin/database-backup/restore", { method: "POST", body: payload });
}

export function getAdminBookMemory(bookId) {
  return apiFetch(`/api/v1/admin/books/${bookId}/memory`);
}

export function changeOwnPassword(payload) {
  return apiFetch("/api/v1/auth/change-password", { method: "POST", body: payload });
}

export function listCharacters(bookId) {
  return apiFetch(`/api/v1/books/${bookId}/characters`);
}

export function createCharacter(bookId, payload) {
  return apiFetch(`/api/v1/books/${bookId}/characters`, { method: "POST", body: payload });
}

export function updateCharacter(bookId, characterId, payload) {
  return apiFetch(`/api/v1/books/${bookId}/characters/${characterId}`, {
    method: "PUT",
    body: payload,
  });
}

export function deleteCharacter(bookId, characterId) {
  return apiFetch(`/api/v1/books/${bookId}/characters/${characterId}`, { method: "DELETE" });
}

export function deleteAllCharacters(bookId) {
  return apiFetch(`/api/v1/books/${bookId}/characters`, { method: "DELETE" });
}

export function listRelations(bookId) {
  return apiFetch(`/api/v1/books/${bookId}/relations`);
}

export function getRelation(bookId, relationId) {
  return apiFetch(`/api/v1/books/${bookId}/relations/${relationId}`);
}

export function createRelation(bookId, payload) {
  return apiFetch(`/api/v1/books/${bookId}/relations`, { method: "POST", body: payload });
}

export function updateRelation(bookId, relationId, payload) {
  return apiFetch(`/api/v1/books/${bookId}/relations/${relationId}`, {
    method: "PUT",
    body: payload,
  });
}

export function deleteRelation(bookId, relationId) {
  return apiFetch(`/api/v1/books/${bookId}/relations/${relationId}`, { method: "DELETE" });
}

export function deleteAllRelations(bookId) {
  return apiFetch(`/api/v1/books/${bookId}/relations`, { method: "DELETE" });
}

export function listRelationEvents(bookId, relationId) {
  return apiFetch(`/api/v1/books/${bookId}/relations/${relationId}/events`);
}

export function listFactions(bookId) {
  return apiFetch(`/api/v1/books/${bookId}/factions`);
}

export function getFaction(bookId, factionId) {
  return apiFetch(`/api/v1/books/${bookId}/factions/${factionId}`);
}

export function createFaction(bookId, payload) {
  return apiFetch(`/api/v1/books/${bookId}/factions`, { method: "POST", body: payload });
}

export function updateFaction(bookId, factionId, payload) {
  return apiFetch(`/api/v1/books/${bookId}/factions/${factionId}`, { method: "PUT", body: payload });
}

export function deleteFaction(bookId, factionId) {
  return apiFetch(`/api/v1/books/${bookId}/factions/${factionId}`, { method: "DELETE" });
}

export function listFactionMemberships(bookId) {
  return apiFetch(`/api/v1/books/${bookId}/faction-memberships`);
}

export function createFactionMembership(bookId, payload) {
  return apiFetch(`/api/v1/books/${bookId}/faction-memberships`, { method: "POST", body: payload });
}

export function updateFactionMembership(bookId, membershipId, payload) {
  return apiFetch(`/api/v1/books/${bookId}/faction-memberships/${membershipId}`, {
    method: "PUT",
    body: payload,
  });
}

export function deleteFactionMembership(bookId, membershipId) {
  return apiFetch(`/api/v1/books/${bookId}/faction-memberships/${membershipId}`, { method: "DELETE" });
}
