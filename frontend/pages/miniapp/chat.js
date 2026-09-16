/* ============================================================
   终端用户端 · 与玩具对话
   ------------------------------------------------------------
   真实链路（契约见 docs/08-AI能力接入设计.md 与 P8 任务书）：

     主通道   ws://<host>/ws/miniapp/chat?token=<accessToken>&deviceId=<id>
     降级通道 POST /miniapp/chat/stream（text/event-stream）

   帧协议（收）：
     session.ready { sessionId }                → 会话建立
     assistant.delta { delta, index }           → 流式文字片段
     assistant.audio { data(base64), mimeType } → 语音片段
     assistant.done  { messageId, latencyMs }   → 本轮结束
     error           { code, message, traceId } → 错误帧

   为什么复用 shared/core/ws.js 而不是自己写一套
   --------------------------------------------
   ChatSocket 已经把「自动重连（指数退避）+ 帧分发 + 未连接时排队补发
   + 会话 ID 维护 + close 语义」都做完了，这些是 WebSocket 客户端里最
   容易写错的部分。本页只做两件它没做的事：

     1. **连接地址与令牌来源**（见 EndUserChatSocket 的注释）；
     2. **降级通道**：契约里的降级端点是 POST，而 ws.js 的 SSE 降级用
        EventSource（只能 GET、无法携带用户文本），因此这里改成
        fetch + ReadableStream 读取 POST 响应流。

   流式渲染：assistant.delta 逐段 textContent += delta，
   而不是每来一段就重建气泡——重建会丢滚动位置、也会闪。
   ============================================================ */

import endUser from '/shared/core/end-user.js';
import { ChatSocket, WS_STATE } from '/shared/core/ws.js';
import { fromHtml, h } from '/shared/ui/dom.js';
import { alert, tag } from '/shared/ui/components.js';
import { icon } from '/shared/ui/icons.js';
import toast from '/shared/ui/toast.js';
import { deviceName, mpApi, navigate, notifyError, registerScreen, state } from './shell.js';

/** 对话 WebSocket 路径（终端用户端专属；令牌走 query，不走 header） */
const CHAT_WS_PATH = '/ws/miniapp/chat';

/**
 * 服务端语义化 close code（见 backend/app/realtime/ws_chat.py）。
 *
 * 为什么必须单独处理：这几种情况**重连没有意义**——令牌错了重连一百次
 * 还是错。若不识别，界面只会显示一句含糊的「连接断开，正在重连…」，
 * 用户永远等不到结果。
 */
const WS_CLOSE_UNAUTHORIZED = 4401;
const WS_CLOSE_FORBIDDEN = 4403;
const WS_CLOSE_PROTOCOL_ERROR = 4400;

/** 快捷提问：这三句正是离线模拟引擎的规则关键词，演示时一定能命中 */
const QUICK_REPLIES = ['讲个故事', '唱首歌', '今天天气怎么样'];

/** 连接状态 → 界面文案 */
const STATE_TEXT = {
  [WS_STATE.IDLE]: '等待连接…',
  [WS_STATE.CONNECTING]: '正在连接设备云…',
  [WS_STATE.OPEN]: '已连接设备云',
  [WS_STATE.RECONNECTING]: '连接断开，正在重连…',
  [WS_STATE.FALLBACK]: '实时连接不可用，已降级为 HTTP 流式',
  [WS_STATE.CLOSED]: '连接已关闭，发送时将改用 HTTP 流式',
};

/* ------------------------------------------------------------
   一、终端用户版 ChatSocket
   ------------------------------------------------------------
   共享实现与本端契约有两处不符，且共享模块本轮不允许修改：

     a) URL：ws.js 用 apiUrl() 拼路径 → 会得到 /api/v1/ws/chat，
        而契约端点在 /ws/miniapp/chat（**不在 API 前缀下**）；
     b) 令牌：ws.js 把 auth.accessToken（后台管理端令牌）放进
        session.open 帧；本端要求令牌走 query，且必须是终端用户令牌。

   因此这里继承 ChatSocket，只覆盖连接过程（connect / connectSse），
   帧分发、排队补发、重连退避、sendText/close 全部沿用父类实现。
   ------------------------------------------------------------ */

class EndUserChatSocket extends ChatSocket {
  /**
   * @param {object} o
   * @param {string} o.deviceId
   * @param {string} o.token 终端用户令牌
   * @param {(code:number) => boolean} [o.onFatalClose]
   *        收到语义化 close code（4401/4403/4400）时的回调。
   *        返回 true 表示「已处理，不要重连」。
   */
  constructor({ deviceId, token, onFatalClose }) {
    super({ deviceId });
    this.token = token || '';
    this.onFatalClose = onFatalClose || null;
  }

  /** 契约要求的连接地址：令牌与设备 ID 都走 query */
  buildUrl() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const query = new URLSearchParams({
      token: this.token,
      deviceId: this.deviceId || '',
    });
    return `${protocol}//${window.location.host}${CHAT_WS_PATH}?${query.toString()}`;
  }

  connect() {
    if (this.state === WS_STATE.OPEN || this.state === WS_STATE.CONNECTING) return;
    this._manualClose = false;

    if (typeof WebSocket === 'undefined') {
      this.connectSse();
      return;
    }

    this._setState(WS_STATE.CONNECTING);

    let socket;
    try {
      socket = new WebSocket(this.buildUrl());
    } catch {
      this.connectSse();
      return;
    }

    this.socket = socket;

    socket.addEventListener('open', () => {
      this.attempts = 0;
      this._setState(WS_STATE.OPEN);
      // 令牌已随 query 上行，session.open 只需声明设备
      this._send({ type: 'session.open', deviceId: this.deviceId });
      while (this._queue.length) this._send(this._queue.shift());
    });

    socket.addEventListener('message', (event) => this._handleMessage(event.data));

    socket.addEventListener('error', () => {
      /* 错误详情由 close 事件统一处理，这里无需重复上报 */
    });

    socket.addEventListener('close', (event) => {
      this.socket = null;
      if (this._manualClose) {
        this._setState(WS_STATE.CLOSED);
        return;
      }
      // 服务端语义化关闭（4400 协议错 / 4401 令牌失效 / 4403 设备不属于本账号）：
      // 重连没有意义，交给页面处理
      if (
        event.code === WS_CLOSE_UNAUTHORIZED ||
        event.code === WS_CLOSE_FORBIDDEN ||
        event.code === WS_CLOSE_PROTOCOL_ERROR
      ) {
        this._setState(WS_STATE.CLOSED);
        if (this.onFatalClose?.(event.code)) return;
      }
      // 首次就握不上手（服务端没有该 WS 路由 / 被代理拒绝）→ 直接降级，
      // 不要对着一个不存在的端点反复重连
      if (this.attempts === 0 && event.code !== 1000 && event.code !== 1001) {
        this.connectSse();
        return;
      }
      this._scheduleReconnect();
    });
  }

  /**
   * SSE 降级：父类用 EventSource（GET），无法携带 user.text；
   * 契约的降级端点是 POST /miniapp/chat/stream。
   * 因此这里**不建立 EventSource**，只把状态切到「降级」，
   * 由本页面的 send() 改走 HTTP 流式请求。
   */
  connectSse() {
    this._setState(WS_STATE.FALLBACK);
  }
}

/* ------------------------------------------------------------
   二、屏幕
   ------------------------------------------------------------ */

/** 当前会话（模块级：onLeave 时关闭，避免切屏后连接还挂着） */
let session = null;

/** 头像节点（图标用 template 构造，不拼 innerHTML） */
function avatarNode(role) {
  const node = h('span', { class: 'mp-msg-avatar' });
  const tpl = document.createElement('template');
  tpl.innerHTML = icon(role === 'assistant' ? 'sparkles' : 'user', { size: 15 }).trim();
  node.append(tpl.content);
  return node;
}

registerScreen('chat', {
  title: '和玩具说话',
  backTo: 'home',
  tab: 'chat',
  showTab: true,
  render: (body, ctx) => {
    /* 防御性清理：万一上次没有走 onLeave（例如异常跳转），先关掉旧连接 */
    session?.close?.();

    const device = state.device;
    const messages = h('div', { class: 'mp-chat-body' });

    if (!device?.id) {
      const section = h('div', { class: 'mp-section' });
      section.append(
        fromHtml(
          alert({
            tone: 'warning',
            title: '还没有可对话的设备',
            text: '对话需要一台已激活并绑定到你的账号的设备。请先扫码激活。',
          }),
        ),
      );
      const toScan = h('button', { class: 'mp-btn mt-4', type: 'button', text: '去扫码' });
      toScan.addEventListener('click', () => navigate('scan'));
      section.append(toScan);
      body.append(section);
      return;
    }

    /* ---- 状态条 ---- */
    const statusEl = h('div', {
      class: 'text-xs text-secondary',
      style: { padding: 'var(--space-2) var(--space-4)', textAlign: 'center' },
      text: STATE_TEXT[WS_STATE.IDLE],
    });

    const setStatus = (state_, extra = '') => {
      const text = STATE_TEXT[state_] || state_;
      statusEl.textContent = extra ? `${text}　${extra}` : text;
    };

    /* ---- 消息区 ---- */
    function scrollDown() {
      body.scrollTop = body.scrollHeight;
    }

    function addMessage(text, role) {
      const bubble = h('div', { class: 'mp-bubble', text });
      messages.append(h('div', { class: `mp-msg mp-msg-${role}` }, avatarNode(role), bubble));
      scrollDown();
      return bubble;
    }

    /** 正在输入指示（首个 delta 到达后移除） */
    function showTyping() {
      const typing = h(
        'div',
        { class: 'mp-msg mp-msg-assistant' },
        avatarNode('assistant'),
        h('div', { class: 'mp-bubble mp-typing' }, h('span'), h('span'), h('span')),
      );
      messages.append(typing);
      scrollDown();
      return typing;
    }

    /* 开场白（本地文案，不走接口）：第一屏不至于空白 */
    addMessage('你好呀！我是你的小伙伴，想听故事、听儿歌，还是聊聊天？', 'assistant');

    /* ---- 流式回复的气泡管理 ---- */
    let typing = null;
    let streaming = null;

    function ensureStreaming() {
      if (streaming) return streaming;
      typing?.remove();
      typing = null;

      const textNode = h('span');
      const metaHost = h('div', { style: { whiteSpace: 'normal' } });
      const bubble = h('div', { class: 'mp-bubble' }, textNode, metaHost);
      messages.append(h('div', { class: 'mp-msg mp-msg-assistant' }, avatarNode('assistant'), bubble));
      scrollDown();

      streaming = { textNode, metaHost, bubble, text: '' };
      return streaming;
    }

    /** 收尾：补上延迟、内容安全标记与会话 ID */
    function finishStream(frame = {}) {
      const current = ensureStreaming();
      typing?.remove();
      typing = null;

      if (!current.text) {
        current.textNode.textContent = '（设备云没有返回内容）';
      }

      // SSE 降级流不带 session.ready，会话 ID 只在 done 帧里出现 → 记下来供续聊
      if (frame.sessionId && session) session.sessionId = frame.sessionId;

      const meta = [];
      // 首字延迟与端到端耗时是两个不同指标（后端都给了，就都显示）
      if (Number.isFinite(Number(frame.latencyMs))) meta.push(`首字 ${Number(frame.latencyMs)} ms`);
      if (Number.isFinite(Number(frame.totalLatencyMs))) {
        meta.push(`端到端 ${Number(frame.totalLatencyMs)} ms`);
      }
      if (frame.messageId) meta.push(`消息 ${frame.messageId}`);

      /* 内容安全：后端在 assistant.done 帧带 safetyFlag / blocked
         （见 app/services/dialogue_service.frame_done），命中时整段回复
         已被替换为安全话术，因此这里要标注，而不是当成正常回答。 */
      const safetyHit = frame.safetyFlag ?? frame.blocked ?? null;
      if (safetyHit) {
        current.bubble.style.background = 'var(--warning-50)';
        current.bubble.style.borderLeft = '3px solid var(--warning-500)';
        current.metaHost.append(
          h('div', { class: 'mt-2' }, fromHtml(tag(`内容安全拦截：已按策略过滤（${safetyHit}）`, 'warning'))),
        );
      }

      if (meta.length) {
        current.metaHost.append(
          h('div', {
            class: 'text-xs text-secondary mt-1',
            style: { whiteSpace: 'normal', fontFamily: 'var(--font-mono)' },
            text: meta.join(' · '),
          }),
        );
      }

      streaming = null;
      scrollDown();
    }

    /** 错误帧（后端主动下发的 { type:'error', code, message, traceId }） */
    function showFrameError(frame) {
      typing?.remove();
      typing = null;
      streaming = null;

      const bubble = h('div', { class: 'mp-bubble' });
      bubble.style.background = 'var(--danger-50)';
      bubble.style.borderLeft = '3px solid var(--danger-500)';
      bubble.append(
        h('div', { class: 'font-semibold', text: `对话失败（${frame.code || 'UNKNOWN'}）` }),
        h('div', { class: 'mt-1', text: frame.message || '设备云返回了错误' }),
      );
      if (frame.traceId) {
        bubble.append(
          h('div', { class: 'text-xs mt-1 mono', style: { opacity: '0.7' }, text: `traceId: ${frame.traceId}` }),
        );
      }
      messages.append(h('div', { class: 'mp-msg mp-msg-assistant' }, avatarNode('assistant'), bubble));
      scrollDown();
    }

    /* ---- 帧分发：WS 与 HTTP 流式共用同一条处理链 ---- */
    function handleFrame(frame) {
      if (!frame || typeof frame !== 'object') return;

      switch (frame.type) {
        case 'session.ready':
          session && (session.sessionId = frame.sessionId || null);
          setStatus(WS_STATE.OPEN, frame.sessionId ? `会话 ${frame.sessionId}` : '');
          break;

        case 'assistant.delta': {
          // 后端帧字段为 delta（schema SERVER_FRAME_FIELDS），
          // 兼容读取 text 是为了不依赖某一个版本的服务端
          const delta = frame.delta ?? frame.text ?? '';
          if (!delta) break;
          typing?.remove();
          typing = null;
          const current = ensureStreaming();
          current.text += String(delta);
          current.textNode.textContent = current.text;
          scrollDown();
          break;
        }

        case 'asr.partial':
          /* 仅音频输入会产生本帧（本页只发 user.text，不发送 user.audio）。
             显式忽略而不是落进 default，是为了让「支持哪些帧」在代码里可读。 */
          break;

        case 'assistant.audio':
          /* 语音片段（base64）：本页只做文字流式，收到即忽略。
             真机联调时在这里接 Audio 播放，见报告「未实现项」。 */
          break;

        case 'assistant.done':
          finishStream(frame);
          break;

        case 'error':
          showFrameError(frame);
          // 帧本身就有 code / message / traceId，与 ApiError 同形，
          // 因此直接复用统一提示（能带上「厂商未配置」这类可执行建议）
          notifyError(frame, '对话失败');
          break;

        default:
          break;
      }
    }

    /* ---- HTTP 流式降级：POST + 读取响应流 ---- */
    async function streamViaHttp(text) {
      // 已离开本屏（onLeave 清空了 session）：丢弃这次发送，不再建流
      if (!session) return;

      const controller = new AbortController();
      session.abort = controller;
      typing = showTyping();

      try {
        const response = await mpApi.raw('POST', '/miniapp/chat/stream', {
          body: { deviceId: device.id, text, sessionId: session.sessionId },
          timeout: 120000,
          signal: controller.signal,
        });

        if (!response.body?.getReader) {
          typing?.remove();
          typing = null;
          throw new Error('当前浏览器不支持流式读取响应');
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let doneSeen = false;

        for (;;) {
          const chunk = await reader.read();
          if (chunk.done) break;
          buffer += decoder.decode(chunk.value, { stream: true });

          // SSE：事件之间以空行分隔，一个事件可有多行 data:
          let boundary = buffer.indexOf('\n\n');
          while (boundary >= 0) {
            const block = buffer.slice(0, boundary);
            buffer = buffer.slice(boundary + 2);

            const data = block
              .split('\n')
              .filter((line) => line.startsWith('data:'))
              .map((line) => line.slice(5).trim())
              .join('\n');

            if (data) {
              try {
                const frame = JSON.parse(data);
                if (frame?.type === 'assistant.done') doneSeen = true;
                handleFrame(frame);
              } catch {
                /* 非 JSON 的心跳/注释行，忽略即可 */
              }
            }
            boundary = buffer.indexOf('\n\n');
          }
        }

        // 服务端没发 done 就断流：也要收尾，否则「正在输入」会一直转
        if (!doneSeen) finishStream({});
      } catch (error) {
        typing?.remove();
        typing = null;
        streaming = null;
        if (error?.name === 'AbortError') return;
        notifyError(error, '对话失败');
        setStatus(WS_STATE.FALLBACK, '（HTTP 流式请求失败）');
      } finally {
        session && (session.abort = null);
      }
    }

    /* ---- 发送 ---- */
    const input = h('input', {
      class: 'input',
      type: 'text',
      placeholder: '说点什么…',
      'aria-label': '消息输入',
    });
    const sendBtn = h('button', { class: 'mp-send', type: 'button', 'aria-label': '发送', text: '↑' });

    function send(rawText) {
      const text = String(rawText ?? '').trim();
      if (!text) return;

      addMessage(text, 'user');
      input.value = '';

      const socket = session?.socket;
      // CLOSED / FALLBACK 说明长连接没得用（或被降级），走 POST 流式通道；
      // CONNECTING / RECONNECTING 则交给 ws.js 的排队机制，握手成功后自动补发
      if (!socket || socket.state === WS_STATE.CLOSED || socket.state === WS_STATE.FALLBACK) {
        streamViaHttp(text);
        return;
      }

      typing = showTyping();
      // 未连上时 ws.js 会先排队，握手成功后自动补发
      socket.sendText(text);
    }

    sendBtn.addEventListener('click', () => send(input.value));
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') send(input.value);
    });

    /* ---- 快捷提问 ---- */
    const quick = h('div', { class: 'mp-quick-replies' });
    QUICK_REPLIES.forEach((text) => {
      const chip = h('div', { class: 'mp-quick-reply', text });
      chip.addEventListener('click', () => send(text));
      quick.append(chip);
    });

    const inputbar = h('div', { class: 'mp-inputbar' }, input, sendBtn);

    body.append(statusEl, messages, quick, inputbar);

    /* ---- 建立连接 ---- */
    const socket = new EndUserChatSocket({
      deviceId: device.id,
      token: endUser.token,
      /**
       * 服务端语义化关闭：
       *   4401 令牌缺失/无效/过期 → 清会话回登录页
       *   4403 设备不属于当前账号 → 说明原因并回首页
       *   4400 帧非法 → 属于前端 bug，提示后停留在本页（有 traceId 可查）
       */
      onFatalClose: (code) => {
        if (code === WS_CLOSE_UNAUTHORIZED) {
          endUser.clear();
          toast.error('登录态已失效，请重新登录');
          navigate('login');
          return true;
        }
        if (code === WS_CLOSE_FORBIDDEN) {
          toast.error('这台设备不属于当前账号，无法对话');
          navigate('home');
          return true;
        }
        toast.error('对话协议异常（4400），请刷新页面后重试');
        return false;
      },
    });
    session = { socket, sessionId: null, abort: null };

    socket.on('*', (payload) => handleFrame(payload));

    /** 只在**首次**连上时打招呼：断线重连不该重复刷屏 */
    let announced = false;
    socket.onStateChange(({ state: next }) => {
      setStatus(next);

      const degraded = next === WS_STATE.FALLBACK;
      if (degraded) toast.warning('实时连接不可用，已降级为 HTTP 流式');

      const firstConnect = next === WS_STATE.OPEN && !announced;
      if (firstConnect) {
        announced = true;
        addMessage(`已连接设备 ${deviceName(device)}，可以开始说话了。`, 'assistant');
      }
    });

    socket.connect();
    setStatus(socket.state);

    /* 从首页「听故事 / 听音乐」进来时带着预置话术：填进输入框，
       让用户自己决定发不发（自动发送会让人莫名其妙） */
    const preset = ctx?.options?.preset;
    if (preset) {
      input.value = preset;
      input.focus();
    }

    // 清理函数挂在 session 上：onLeave 与下次 render 都可能用到
    session.close = () => {
      session?.abort?.abort?.();
      socket.close();
      session = null;
    };
  },
  onLeave: () => {
    session?.close?.();
    session = null;
  },
});
