/* ============================================================
   HTTP 客户端（全端统一契约）
   ------------------------------------------------------------
   本文件是前后端之间的**唯一**通信入口，契约在此定死：

   请求
     * 自动注入 Authorization: Bearer <accessToken>
     * 自动注入 x-trace-id（便于与后端日志、审计串联）
     * JSON 请求体自动序列化并设置 Content-Type
     * 支持 AbortController 超时

   响应
     * 2xx → 直接返回解析后的数据
     * 非 2xx → 抛出 ApiError，字段：code / message / traceId / details / status
     * 后端统一错误体：{ code, message, traceId, details? }

   令牌过期
     * 401 且本地有刷新令牌时，自动刷新一次并重放原请求
     * 刷新失败 → 触发未授权处理器（跳登录页）
     * 刷新请求本身失败不递归重试（防止死循环）
   ============================================================ */

import { apiUrl, config } from './config.js';
import auth from './auth.js';

/* ------------------------------------------------------------
   一、错误类型
   ------------------------------------------------------------ */

/** 后端统一错误码（与 app/core/errors.py 的 ErrorCode 保持一致） */
export const ERROR_CODES = {
  UNAUTHENTICATED: 'UNAUTHENTICATED',
  INVALID_CREDENTIALS: 'INVALID_CREDENTIALS',
  ACCOUNT_DISABLED: 'ACCOUNT_DISABLED',
  ACCOUNT_LOCKED: 'ACCOUNT_LOCKED',
  PASSWORD_CHANGE_REQUIRED: 'PASSWORD_CHANGE_REQUIRED',
  PERMISSION_DENIED: 'PERMISSION_DENIED',
  TENANT_DISABLED: 'TENANT_DISABLED',
  TENANT_CODE_EXISTS: 'TENANT_CODE_EXISTS',
  VALIDATION_ERROR: 'VALIDATION_ERROR',
  RESOURCE_NOT_FOUND: 'RESOURCE_NOT_FOUND',
  CASCADE_CONFLICT: 'CASCADE_CONFLICT',
  IDEMPOTENCY_CONFLICT: 'IDEMPOTENCY_CONFLICT',
  PRODUCT_CODE_EXISTS: 'PRODUCT_CODE_EXISTS',
  PRODUCT_NOT_AUTHORIZED: 'PRODUCT_NOT_AUTHORIZED',
  DEVICE_NOT_AVAILABLE: 'DEVICE_NOT_AVAILABLE',
  DEVICE_NOT_FOUND: 'DEVICE_NOT_FOUND',
  DEVICE_ALREADY_BOUND: 'DEVICE_ALREADY_BOUND',
  DEVICE_FROZEN: 'DEVICE_FROZEN',
  DEVICE_NOT_IN_TENANT: 'DEVICE_NOT_IN_TENANT',
  INVALID_STATE_TRANSITION: 'INVALID_STATE_TRANSITION',
  QR_INVALID: 'QR_INVALID',
  QR_EXPIRED: 'QR_EXPIRED',
  BIND_FAILED: 'BIND_FAILED',
  VENDOR_UNAVAILABLE: 'VENDOR_UNAVAILABLE',
  INTERNAL_ERROR: 'INTERNAL_ERROR',
  /** 前端本地产生的错误码 */
  NETWORK_ERROR: 'NETWORK_ERROR',
  TIMEOUT: 'TIMEOUT',
};

export class ApiError extends Error {
  constructor({ code, message, traceId, details, status }) {
    super(message || '请求失败');
    this.name = 'ApiError';
    this.code = code || ERROR_CODES.INTERNAL_ERROR;
    this.traceId = traceId || null;
    this.details = details ?? null;
    this.status = status ?? 0;
  }

  /** 是否为网络层错误（无 HTTP 响应） */
  get isNetworkError() {
    return this.code === ERROR_CODES.NETWORK_ERROR || this.code === ERROR_CODES.TIMEOUT;
  }

  /** 是否为认证相关错误 */
  get isAuthError() {
    return [
      ERROR_CODES.UNAUTHENTICATED,
      ERROR_CODES.INVALID_CREDENTIALS,
      ERROR_CODES.ACCOUNT_LOCKED,
      ERROR_CODES.ACCOUNT_DISABLED,
    ].includes(this.code);
  }

  /** 供日志与提示使用的可读描述 */
  get display() {
    return this.traceId ? `${this.message}（traceId: ${this.traceId}）` : this.message;
  }
}

/* ------------------------------------------------------------
   二、可注入的处理器
   ------------------------------------------------------------ */

let onUnauthorized = null;
let onError = null;

/** 设置 401 处理（通常由应用壳注入：清理状态并跳转登录页） */
export function setUnauthorizedHandler(handler) {
  onUnauthorized = handler;
}

/** 设置全局错误观察者（用于统一 toast 提示） */
export function setErrorHandler(handler) {
  onError = handler;
}

/* ------------------------------------------------------------
   三、辅助
   ------------------------------------------------------------ */

/** 生成 traceId（与后端格式一致：32 位十六进制） */
function newTraceId() {
  if (window.crypto?.randomUUID) {
    return window.crypto.randomUUID().replace(/-/g, '');
  }
  return Array.from({ length: 32 }, () => Math.floor(Math.random() * 16).toString(16)).join('');
}

/** 构造查询串 */
function buildQuery(params) {
  if (!params) return '';
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === '') continue;
    if (Array.isArray(value)) {
      value.forEach((item) => {
        if (item !== null && item !== undefined && item !== '') search.append(key, item);
      });
    } else {
      search.append(key, String(value));
    }
  }
  const query = search.toString();
  return query ? `?${query}` : '';
}

/** 解析响应体（容错非 JSON 响应） */
async function parseBody(response) {
  const contentType = response.headers.get('content-type') || '';
  if (response.status === 204) return null;

  if (contentType.includes('application/json')) {
    try {
      return await response.json();
    } catch {
      return null;
    }
  }

  try {
    const text = await response.text();
    return text || null;
  } catch {
    return null;
  }
}

/** 从响应体构造 ApiError */
function toApiError(response, body, fallbackMessage) {
  if (body && typeof body === 'object' && (body.code || body.message)) {
    return new ApiError({
      code: body.code || ERROR_CODES.INTERNAL_ERROR,
      message: body.message || fallbackMessage,
      traceId: body.traceId || response.headers.get('x-trace-id'),
      details: body.details,
      status: response.status,
    });
  }
  return new ApiError({
    code: response.status === 404 ? ERROR_CODES.RESOURCE_NOT_FOUND : ERROR_CODES.INTERNAL_ERROR,
    message: fallbackMessage || `请求失败（HTTP ${response.status}）`,
    traceId: response.headers.get('x-trace-id'),
    status: response.status,
  });
}

/* ------------------------------------------------------------
   四、刷新令牌（单飞：并发请求只触发一次刷新）
   ------------------------------------------------------------ */

let refreshPromise = null;

async function refreshAccessToken() {
  const refreshToken = auth.refreshToken;
  if (!refreshToken) throw new ApiError({ code: ERROR_CODES.UNAUTHENTICATED, message: '登录态已失效，请重新登录' });

  if (!refreshPromise) {
    refreshPromise = (async () => {
      const response = await fetch(apiUrl('/auth/refresh'), {
        method: 'POST',
        headers: { 'content-type': 'application/json', 'x-trace-id': newTraceId() },
        body: JSON.stringify({ refreshToken }),
      });
      const body = await parseBody(response);
      if (!response.ok) throw toApiError(response, body, '刷新登录态失败');
      auth.updateTokens(body);
      return body.accessToken;
    })().finally(() => {
      refreshPromise = null;
    });
  }

  return refreshPromise;
}

/* ------------------------------------------------------------
   五、核心请求
   ------------------------------------------------------------ */

/**
 * 发起请求。
 * @param {string} method
 * @param {string} path
 * @param {object} [options]
 * @param {object} [options.params] 查询参数
 * @param {any}    [options.body]   请求体（对象自动 JSON 序列化）
 * @param {boolean}[options.raw]    true → 返回完整 Response，不做错误处理
 * @param {boolean}[options.silent] true → 不触发全局错误处理器
 * @param {string} [options.idempotencyKey] 幂等键（写操作防重复提交）
 * @param {object} [options.headers]
 * @param {AbortSignal} [options.signal]
 * @param {number} [options.timeout]
 * @param {boolean}[options.retryOn401] 内部使用，防止刷新后再次 401 造成死循环
 */
async function request(method, path, options = {}) {
  const {
    params,
    body,
    headers: extraHeaders,
    signal,
    timeout = config.requestTimeout,
    idempotencyKey,
    silent = false,
    retryOn401 = true,
  } = options;

  const headers = { 'x-trace-id': newTraceId(), ...(extraHeaders || {}) };

  if (body !== undefined && body !== null && !(body instanceof FormData)) {
    headers['content-type'] = headers['content-type'] || 'application/json';
  }

  if (auth.accessToken) {
    headers.authorization = `Bearer ${auth.accessToken}`;
  }

  if (idempotencyKey) {
    headers['idempotency-key'] = idempotencyKey;
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort('timeout'), timeout);

  // 外部取消信号与内部超时信号联动
  if (signal) {
    if (signal.aborted) controller.abort(signal.reason);
    else signal.addEventListener('abort', () => controller.abort(signal.reason), { once: true });
  }

  let response;
  try {
    response = await fetch(apiUrl(path) + buildQuery(params), {
      method,
      headers,
      body:
        body === undefined || body === null
          ? undefined
          : body instanceof FormData
            ? body
            : JSON.stringify(body),
      signal: controller.signal,
    });
  } catch (error) {
    clearTimeout(timer);
    const aborted = controller.signal.aborted;
    const isTimeout = aborted && controller.signal.reason === 'timeout';
    const apiError = new ApiError({
      code: isTimeout ? ERROR_CODES.TIMEOUT : ERROR_CODES.NETWORK_ERROR,
      message: isTimeout ? '请求超时，请检查网络后重试' : '网络异常，请检查网络连接',
      traceId: headers['x-trace-id'],
    });
    if (!silent && onError) onError(apiError);
    throw apiError;
  } finally {
    clearTimeout(timer);
  }

  const payload = await parseBody(response);

  /* ---- 成功 ---- */
  if (response.ok) return payload;

  /* ---- 401：尝试刷新一次并重放 ---- */
  if (response.status === 401 && retryOn401 && auth.refreshToken) {
    try {
      await refreshAccessToken();
      return await request(method, path, { ...options, retryOn401: false });
    } catch {
      auth.clear();
      const unauthorized = new ApiError({
        code: ERROR_CODES.UNAUTHENTICATED,
        message: '登录态已失效，请重新登录',
        traceId: response.headers.get('x-trace-id'),
        status: 401,
      });
      if (onUnauthorized) onUnauthorized(unauthorized);
      throw unauthorized;
    }
  }

  /* ---- 其他错误 ---- */
  const apiError = toApiError(response, payload, null);

  if (response.status === 401 && onUnauthorized) {
    auth.clear();
    onUnauthorized(apiError);
  }

  if (!silent && onError) onError(apiError);
  throw apiError;
}

/* ------------------------------------------------------------
   六、对外接口
   ------------------------------------------------------------ */

export const api = {
  /** GET */
  get: (path, options) => request('GET', path, options),

  /** POST */
  post: (path, body, options) => request('POST', path, { ...options, body }),

  /** PUT */
  put: (path, body, options) => request('PUT', path, { ...options, body }),

  /** PATCH */
  patch: (path, body, options) => request('PATCH', path, { ...options, body }),

  /** DELETE（部分接口需要请求体，故也接受 body） */
  del: (path, body, options) => request('DELETE', path, { ...options, body }),

  /**
   * 文件上传（multipart/form-data）
   * 不设置 content-type，交给浏览器自动带 boundary
   */
  upload: (path, formData, options) => request('POST', path, { ...options, body: formData, timeout: 120000 }),

  /** 原始请求（需要读取响应头时使用） */
  raw: (method, path, options) => request(method, path, options),

  /** 生成幂等键（写操作防重复提交） */
  newIdempotencyKey: () => newTraceId(),
};

export default api;
