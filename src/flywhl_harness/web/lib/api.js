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
  patch: (p, b, o) => req("PATCH", p, b || {}, o),
  /** 下载附件（导出的 Markdown 等）：先取回再保存，出错时给出说明，而不是把浏览器带到一个 JSON 错误页 */
  async download(path, fallbackName) {
    let r;
    try { r = await fetch(path, { credentials: "same-origin" }); }
    catch { throw new ApiError("无法连接到 flywhl 服务，请确认服务仍在运行", 0); }
    if (!r.ok) throw new ApiError(r.status === 401 ? "访问令牌无效或已失效" : `下载失败（HTTP ${r.status}）`, r.status);
    const m = /filename\*=UTF-8''([^;]+)/i.exec(r.headers.get("content-disposition") || "");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(await r.blob());
    a.download = m ? decodeURIComponent(m[1]) : fallbackName;
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  },
};
