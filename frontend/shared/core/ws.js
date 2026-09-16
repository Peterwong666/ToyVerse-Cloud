/* ============================================================
   实时对话 WebSocket 客户端
   ------------------------------------------------------------
   用途：终端用户端与玩具设备的对话（流式回复）。
   设计要点：
     * 自动重连（指数退避，上限 30 秒）
     * SSE 降级：WebSocket 不可用时改用 /miniapp/chat/stream
     * 帧协议与后端一致（见 docs/08-AI能力接入设计.md）

   帧协议（客户端 → 服务端）
     { type: 'session.open',  deviceId, token }
     { type: 'user.text',     text }
     { type: 'user.audio',    chunk, seq, format }
     { type: 'session.close' }

   帧协议（服务端 → 客户端）
     { type: 'session.ready', sessionId, voice, capabilities, safety }
     { type: 'asr.partial',   text }
     { type: 'assistant.delta', text, seq }
     { type: 'assistant.audio', url | chunk }
     { type: 'assistant.done', messageId, latencyMs }
     { type: 'error',         code, message, traceId }
   ============================================================ */

import { apiUrl, config, isDev } from './config.js';
import auth from './auth.js';

/** 连接状态 */
export const WS_STATE = {
  IDLE: 'idle',
  CONNECTING: 'connecting',
  OPEN: 'open',
  RECONNECTING: 'reconnecting',
  CLOSED: 'closed',
  FALLBACK: 'fallback',
};

/** 重连退避参数 */
const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 30000;

/** 由相对路径推导 WebSocket 地址 */
function resolveWsUrl(path) {
  const base = apiUrl(path);
  if (/^wss?:\/\//i.test(base)) return base;

  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const normalized = base.startsWith('/') ? base : `/${base}`;
  return `${protocol}//${window.location.host}${normalized}`;
}

export class ChatSocket {
  /**
   * @param {object} options
   * @param {string} options.deviceId   设备 ID
   * @param {string} [options.path]     WebSocket 路径
   * @param {string} [options.streamPath] SSE 降级路径
   */
  constructor({ deviceId, path = '/ws/chat', streamPath = '/miniapp/chat/stream' } = {}) {
    this.deviceId = deviceId;
    this.path = path;
    this.streamPath = streamPath;

    this.socket = null;
    this.eventSource = null;
    this.state = WS_STATE.IDLE;
    this.sessionId = null;

    this.attempts = 0;
    this._manualClose = false;
    this._reconnectTimer = null;
    this._handlers = new Map();
    this._queue = [];
  }

  /* ----------------------------------------------------------
     事件订阅
     ---------------------------------------------------------- */

  /**
   * 订阅帧类型。
   * @param {string} type 帧类型；'*' 表示全部
   * @param {Function} handler (payload) => void
   * @returns {Function} 取消订阅
   */
  on(type, handler) {
    if (!this._handlers.has(type)) this._handlers.set(type, new Set());
    this._handlers.get(type).add(handler);
    return () => this._handlers.get(type)?.delete(handler);
  }

  /** 订阅连接状态变化 */
  onStateChange(handler) {
    return this.on('@state', handler);
  }

  _emit(type, payload) {
    this._handlers.get(type)?.forEach((handler) => {
      try {
        handler(payload, type);
      } catch (error) {
        console.error(`[ws] 处理帧 ${type} 失败`, error);
      }
    });
    this._handlers.get('*')?.forEach((handler) => {
      try {
        handler(payload, type);
      } catch (error) {
        console.error('[ws] 通配处理器失败', error);
      }
    });
  }

  _setState(state) {
    if (this.state === state) return;
    this.state = state;
    this._emit('@state', { state });
  }

  /* ----------------------------------------------------------
     连接
     ---------------------------------------------------------- */

  /** 建立连接（自动在 WS 与 SSE 之间选择） */
  connect() {
    if (this.state === WS_STATE.OPEN || this.state === WS_STATE.CONNECTING) return;
    this._manualClose = false;

    if (typeof WebSocket === 'undefined') {
      this.connectSse();
      return;
    }

    this._setState(WS_STATE.CONNECTING);

    const url = `${resolveWsUrl(this.path)}?deviceId=${encodeURIComponent(this.deviceId || '')}`;
    let socket;
    try {
      socket = new WebSocket(url);
    } catch (error) {
      if (isDev) console.warn('[ws] 创建连接失败，降级到 SSE', error);
      this.connectSse();
      return;
    }

    this.socket = socket;

    socket.addEventListener('open', () => {
      this.attempts = 0;
      this._setState(WS_STATE.OPEN);
      this._send({
        type: 'session.open',
        deviceId: this.deviceId,
        token: auth.accessToken,
      });
      // 补发连接期间排队的消息
      while (this._queue.length) this._send(this._queue.shift());
    });

    socket.addEventListener('message', (event) => this._handleMessage(event.data));

    socket.addEventListener('error', (error) => {
      if (isDev) console.warn('[ws] 连接错误', error);
    });

    socket.addEventListener('close', (event) => {
      this.socket = null;
      if (this._manualClose) {
        this._setState(WS_STATE.CLOSED);
        return;
      }
      // 未握手成功即被关闭：说明服务端不支持 WS，降级到 SSE
      if (this.attempts === 0 && event.code !== 1000 && event.code !== 1001) {
        this.connectSse();
        return;
      }
      this._scheduleReconnect();
    });
  }

  /** SSE 降级连接 */
  connectSse() {
    if (typeof EventSource === 'undefined') {
      this._setState(WS_STATE.CLOSED);
      return;
    }

    this._setState(WS_STATE.FALLBACK);
    const url =
      `${apiUrl(this.streamPath)}` +
      `?deviceId=${encodeURIComponent(this.deviceId || '')}` +
      `&token=${encodeURIComponent(auth.accessToken || '')}`;

    this.eventSource = new EventSource(url);
    this.eventSource.addEventListener('message', (event) => this._handleMessage(event.data));
    this.eventSource.addEventListener('error', () => {
      if (this._manualClose) return;
      this.eventSource?.close();
      this.eventSource = null;
      this._scheduleReconnect();
    });
  }

  _scheduleReconnect() {
    if (this._manualClose) return;
    this._setState(WS_STATE.RECONNECTING);

    const delay = Math.min(RECONNECT_BASE_MS * 2 ** this.attempts, RECONNECT_MAX_MS);
    this.attempts += 1;

    clearTimeout(this._reconnectTimer);
    this._reconnectTimer = setTimeout(() => this.connect(), delay);
  }

  /* ----------------------------------------------------------
     收发
     ---------------------------------------------------------- */

  _send(frame) {
    const payload = typeof frame === 'string' ? frame : JSON.stringify(frame);
    if (this.socket && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(payload);
      return true;
    }
    // 未连接：排队，握手成功后补发（上限 20 条，防止无界增长）
    if (this._queue.length < 20) this._queue.push(frame);
    return false;
  }

  _handleMessage(raw) {
    let payload;
    try {
      payload = JSON.parse(raw);
    } catch {
      if (isDev) console.warn('[ws] 收到非 JSON 帧', raw);
      return;
    }

    if (!payload || typeof payload !== 'object') return;

    if (payload.type === 'session.ready') {
      this.sessionId = payload.sessionId || null;
    }

    this._emit(payload.type, payload);
  }

  /* ----------------------------------------------------------
     业务方法
     ---------------------------------------------------------- */

  /** 发送文本并等待流式回复 */
  sendText(text) {
    const value = String(text ?? '').trim();
    if (!value) return false;
    return this._send({ type: 'user.text', text: value });
  }

  /** 发送音频分片 */
  sendAudio(chunk, seq, format = 'pcm16k') {
    return this._send({ type: 'user.audio', chunk, seq, format });
  }

  /** 主动关闭（不再自动重连） */
  close() {
    this._manualClose = true;
    clearTimeout(this._reconnectTimer);
    this._queue.length = 0;

    if (this.socket) {
      try {
        this._send({ type: 'session.close' });
        this.socket.close(1000, 'client closed');
      } catch {
        /* 忽略关闭异常 */
      }
      this.socket = null;
    }

    this.eventSource?.close();
    this.eventSource = null;

    this._setState(WS_STATE.CLOSED);
  }
}

/** 便捷工厂 */
export function createChatSocket(options) {
  return new ChatSocket(options);
}

export default ChatSocket;
