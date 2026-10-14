// HTTP 客户端：同源请求自动带上访问令牌 cookie；错误统一抛出带中文说明的 ApiError。

export class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

async function req(method, path, body, { timeout = 30000 } = {}) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeout);
  let r;
  try {
    r = await fetch(path, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
      signal: ctl.signal,
      credentials: "same-origin",
    });
  } catch (e) {
    throw new ApiError(e.name === "AbortError" ? "请求超时，请稍后重试" : "无法连接到 flywhl 服务，请确认服务仍在运行", 0);
  } finally {
    clearTimeout(timer);
  }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const detail = typeof data.detail === "string" ? data.detail : Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join("；") : "";
    if (r.status === 401) throw new ApiError("访问令牌无效或已失效：请使用启动时打印的链接重新打开", 401);
    throw new ApiError(detail || `请求失败（HTTP ${r.status}）`, r.status);
  }
  return data;
}

export const api = {
  get: (p, o) => req("GET", p, null, o),
  post: (p, b, o) => req("POST", p, b || {}, o),
};
