/* ============================================================
   平台端 · 设备详情
   ------------------------------------------------------------
   路由：`#/devices/:id`（参数化路由，刷新与分享都不丢状态）

   P4 的欠账在这里还清：设备列表的「详情」按钮此前只弹一句提示，
   因为当时还没有详情页；P5 补上本页后，按钮直接跳转到这里。

   三个标签页：
     概览     四维状态（分列）+ 基本信息 + 设备密钥（凭证）
     绑定     当前绑定记录（未绑定则给出「去绑定」的指引）
     事件     设备流转事件时间线（分页，加载更多）

   页面里的两条硬规则
   ------------------
   1. **在线一律看布尔 `online`**（180 秒心跳窗口派生），不看
      `onlineStatus` —— 后者是落库投影，设备掉线后它不会自己变。
      四维标签里仍展示 `onlineStatus`，那只是「哪一维不对」的定位信息。
   2. **设备密钥明文只显示一次**。签发接口的 `secret` 只在响应里出现，
      因此必须立刻用一次性弹窗给出复制入口；明文**绝不**写进
      localStorage、URL 或任何会持久化的地方。
   ============================================================ */

import api from '/shared/core/api.js';
import auth, { PERM } from '/shared/core/auth.js';
import { copyText, esc, formatDate, formatRelative, fromHtml, h, html, raw } from '/shared/ui/dom.js';
import {
  alert,
  descList,
  emptyState,
  loadingState,
  statGrid,
  statusTag,
  tag,
  timeline,
} from '/shared/ui/components.js';
import { table } from '/shared/ui/table.js';
import { createDetailPage } from '/shared/app/page.js';
import { confirmDialog, modal } from '/shared/ui/modal.js';
import toast from '/shared/ui/toast.js';
import {
  ACTIVATION_STATUS_MAP,
  ASSET_STATUS_MAP,
  BIND_STATUS_MAP,
  BINDING_RECORD_STATUS_MAP,
  NETWORK_MAP,
  ONLINE_STATUS_MAP,
  P5_ERROR_HINTS,
  notifyError,
} from './common.js';

/** 可冻结的资产状态（与后端 FREEZABLE_ASSET_STATUSES 对齐，只允许 IN_STOCK） */
const FREEZABLE = new Set(['IN_STOCK']);

/** 事件分页大小 */
const EVENT_PAGE_SIZE = 20;

/** 事件类型 → 中文名（对应后端 DEVICE_EVENT_DIMENSIONS 的键） */
const EVENT_TYPE_LABELS = {
  GENERATED: '生成设备',
  IMPORTED: '批次导入',
  IN_STOCK: '入库',
  ALLOCATED: '分配到租户',
  FROZEN: '冻结',
  THAWED: '解冻',
  RETIRED: '报废',
  BOUND: '绑定',
  UNBOUND: '解绑',
  ACTIVATED: '激活',
  BIND_FAILED: '激活失败',
  HEARTBEAT: '心跳上报',
  ONLINE: '转为在线',
  OFFLINE: '转为离线',
};

/** 事件维度 → 中文名 / 时间线配色（四维正交，颜色用来区分维度而不是严重程度） */
const DIMENSION_LABELS = { asset: '资产', activation: '激活', online: '在线', bind: '绑定' };
const DIMENSION_TONES = { asset: 'brand', activation: 'teal', online: 'info', bind: 'success' };

/* ------------------------------------------------------------
   一、小工具
   ------------------------------------------------------------ */

/** 读取设备详情 */
function loadDevice(id) {
  return api.get(`/platform/devices/${id}`);
}

/**
 * 统一错误提示：P5 新增的错误码优先用 P5_ERROR_HINTS 给出动作建议，
 * 其余交给 common.notifyError（它会用 P3/P4 的既有提示表）。
 */
function notifyDeviceError(error, fallback = '操作失败，请稍后重试') {
  const hint = P5_ERROR_HINTS[error?.code];
  if (hint) {
    toast.error(`${error?.message || fallback}　${hint}`, { traceId: error?.traceId || '' });
    return;
  }
  notifyError(error, fallback);
}

/** 状态码 → 中文（四维任取其一命中即可；用于时间线的 from → to） */
function statusText(code) {
  if (!code) return '';
  return (
    ASSET_STATUS_MAP[code]?.text ||
    ACTIVATION_STATUS_MAP[code]?.text ||
    ONLINE_STATUS_MAP[code]?.text ||
    BIND_STATUS_MAP[code]?.text ||
    code
  );
}

/** 展示用时间（空值给空串，由 descList 统一显示为 '-'） */
function showTime(value) {
  return value ? formatDate(value) : '';
}

/**
 * 需要填写原因的写操作（冻结 / 报废）。
 *
 * 与设备列表页同样的理由：冻结与报废在后端都要记录原因，
 * 先让用户填好再提交，比提交后收到「原因必填」友好得多。
 */
async function runWithReason(o) {
  const { confirmed, reason } = await confirmDialog({
    title: o.title,
    description: o.description,
    detail: o.detail,
    tone: o.tone || 'warning',
    confirmText: o.confirmText,
    requireReason: true,
    reasonLabel: o.reasonLabel,
  });
  if (!confirmed) return;
  try {
    await o.run(reason);
    toast.success(o.successMessage);
    await o.onDone?.();
  } catch (error) {
    notifyDeviceError(error);
  }
}

/**
 * 一次性密钥弹窗。
 *
 * 与 common.showOneTimePassword 同一形态（文案也刻意同样强调
 * 「只显示一次」），但字段标签换成设备语境，因此在这里单独实现，
 * 避免为了复用而把「登录账号 / 初始密码」这类不正确的前缀
 * 显示在设备密钥上。
 */
function showOneTimeSecret({ title = '设备密钥（仅显示一次）', subject, secret }) {
  const body = h('div');
  body.append(fromHtml(descList([['设备 SN', subject], ['设备密钥（明文）', secret]], { cols: 1 })));
  body.append(
    fromHtml(html`<div class="field-hint mt-2">
      该密钥只会显示这一次，关闭后无法再次查看（平台只保存 SHA-256 摘要，不保存明文）。
      请立即复制并写入设备端；若密钥泄露或设备丢失，请重新签发以覆盖旧密钥。
    </div>`),
  );

  const dialog = modal({
    title,
    size: 'sm',
    body,
    footer: (dlg) => {
      const copy = h('button', { class: 'btn btn-primary', type: 'button', text: '复制密钥' });
      copy.addEventListener('click', async () => {
        const ok = await copyText(secret);
        if (ok) toast.success('密钥已复制到剪贴板');
        else toast.warning('复制失败，请手动选中后复制');
      });
      const close = h('button', { class: 'btn', type: 'button', text: '我已保存，关闭' });
      close.addEventListener('click', () => dlg.close());
      return [close, copy];
    },
  });

  return dialog.result;
}

/* ------------------------------------------------------------
   二、写操作（页头动作与概览区的按钮共用）
   ------------------------------------------------------------ */

/** 模拟心跳（演示用：后端响应里 simulated=true，不冒充真实上报） */
async function simulateHeartbeat(device, reload) {
  const handle = toast.loading('正在模拟设备心跳…');
  try {
    const result = await api.post(
      `/platform/devices/${device.id}/simulate-heartbeat`,
      { online: true },
      { idempotencyKey: api.newIdempotencyKey() },
    );
    handle.close();
    toast.success(
      `已写入演示心跳：${result?.online === false ? '设备离线' : '设备在线'}` +
        `${result?.simulated ? '（模拟数据）' : ''}`,
    );
    await reload();
  } catch (error) {
    handle.close();
    notifyDeviceError(error, '模拟心跳失败');
  }
}

/** 签发设备密钥：成功后立刻用一次性弹窗展示明文 */
async function issueCredential(device, reload) {
  const { confirmed } = await confirmDialog({
    title: `为设备 ${device.sn} 签发设备密钥`,
    description: '签发后设备端用该密钥上报心跳；明文密钥只会显示这一次。',
    detail: '续签会覆盖同类型（DEVICE_SECRET）的旧密钥，旧密钥立即失效 —— 设备丢失时正是靠这个把旧密钥作废。',
    tone: 'warning',
    confirmText: '确认签发',
  });
  if (!confirmed) return;

  const handle = toast.loading('正在签发设备密钥…');
  try {
    const result = await api.post(
      `/platform/devices/${device.id}/credentials`,
      { credentialType: 'DEVICE_SECRET' },
      { idempotencyKey: api.newIdempotencyKey() },
    );
    handle.close();
    if (!result?.secret) {
      // 没有明文就没什么可复制的：明确告知，而不是弹一个空弹窗
      toast.warning('签发成功，但后端未返回明文密钥，请重新签发后再试');
      await reload();
    } else {
      // ★ 顺序很重要：**先刷新页面数据、再弹一次性明文**。
      // 反过来的话（早期实现）用户在弹窗打开期间看到的凭证表还是上一轮的掩码，
      // 刚签发的掩码要等关闭弹窗之后才出现 —— 看起来像「签发没生效」，
      // 甚至会被误读成「弹窗里的密钥和页面上的不是同一个」。
      await reload();
      await showOneTimeSecret({ subject: device.sn, secret: result.secret });
    }
  } catch (error) {
    handle.close();
    notifyDeviceError(error, '签发设备密钥失败');
  }
}

/* ------------------------------------------------------------
   三、概览
   ------------------------------------------------------------ */

function renderOverview(host, data, ctx) {
  const { reload, onIssueCredential } = ctx;
  const online = Boolean(data.online);
  const assetText = ASSET_STATUS_MAP[data.assetStatus]?.text || data.assetStatus || '-';
  const activationText =
    ACTIVATION_STATUS_MAP[data.activationStatus]?.text || data.activationStatus || '-';
  const bindText = BIND_STATUS_MAP[data.bindStatus]?.text || data.bindStatus || '-';

  /* ---- 四维状态概览卡 ---- */
  host.append(
    fromHtml(
      statGrid([
        {
          label: '资产状态',
          value: assetText,
          icon: 'device',
          tone: ASSET_STATUS_MAP[data.assetStatus]?.tone || 'brand',
          foot: '维度一：设备本身的生命周期',
        },
        {
          label: '激活状态',
          value: activationText,
          icon: 'checkCircle',
          tone: ACTIVATION_STATUS_MAP[data.activationStatus]?.tone || 'default',
          foot: '维度二：终端用户是否已激活',
        },
        {
          label: '在线',
          value: online ? '在线' : '离线',
          icon: 'activity',
          tone: online ? 'success' : 'warning',
          foot: '维度三：180 秒心跳窗口内（以 online 为准）',
        },
        {
          label: '绑定状态',
          value: bindText,
          icon: 'link',
          tone: BIND_STATUS_MAP[data.bindStatus]?.tone || 'default',
          foot: '维度四：是否已绑定终端用户',
        },
      ]),
    ),
  );

  /* ---- 四维标签（ADR-03：四个维度分列，便于定位「哪一维不对」） ---- */
  const stateCard = h('div', { class: 'card mt-4 p-4' });
  stateCard.append(fromHtml(`<div class="card-title mb-3">四维状态</div>`));
  stateCard.append(
    fromHtml(
      html`<div class="flex items-center gap-2 flex-wrap">
        <span class="text-xs text-secondary">资产</span>${raw(statusTag(data.assetStatus, ASSET_STATUS_MAP))}
        <span class="text-xs text-secondary">激活</span>${raw(statusTag(data.activationStatus, ACTIVATION_STATUS_MAP))}
        <span class="text-xs text-secondary">在线</span>${raw(statusTag(data.onlineStatus, ONLINE_STATUS_MAP))}
        <span class="text-xs text-secondary">绑定</span>${raw(statusTag(data.bindStatus, BIND_STATUS_MAP))}
      </div>`,
    ),
  );
  stateCard.append(
    fromHtml(html`<div class="field-hint mt-3">
      派生标签：<span class="font-semibold">${data.label || '-'}</span>。
      四维是正交的：左起第二个标签（在线）来自落库的 onlineStatus，
      只用于定位与筛选；页面上的「是否在线」结论一律以 180 秒窗口的
      online 为准（见上方的统计卡与下方的「最近心跳」）。
    </div>`),
  );
  host.append(stateCard);

  /* ---- 基本信息 ---- */
  const infoCard = h('div', { class: 'card mt-4' });
  infoCard.append(fromHtml(`<div class="card-head"><div class="card-title">基本信息</div></div>`));
  infoCard.append(
    fromHtml(
      descList([
        ['SN', data.sn],
        ['IMEI', data.imei],
        ['ICCID', data.iccid],
        ['MAC', data.mac],
        ['厂商设备 ID', data.vendorDeviceId],
        ['联网方式', NETWORK_MAP[data.networkType]?.text || data.networkType],
        ['固件版本', data.firmwareVersion],
        ['是否在线', online ? '在线（180 秒心跳窗口内）' : '离线'],
        ['最近心跳', data.lastHeartbeatAt ? formatRelative(data.lastHeartbeatAt) : ''],
        ['归属租户', data.tenantName || '平台自有库存'],
        ['客户产品', data.clientProductName],
        ['订单号', data.orderNo],
        ['生成时间', showTime(data.generatedAt)],
        ['激活时间', showTime(data.activatedAt)],
        ['绑定时间', showTime(data.boundAt)],
        ['冻结时间', showTime(data.frozenAt)],
        ['冻结原因', data.freezeReason],
        ['报废时间', showTime(data.retiredAt)],
        ['报废原因', data.retireReason],
        ['备注', data.remark],
      ]),
    ),
  );
  host.append(infoCard);

  /* ---- 设备密钥（凭证）----
     只展示掩码：明文在签发时一次性返回，后端从不下发明文，
     因此这里没有任何「查看明文」的入口 —— 那正是设计意图。 */
  const credCard = h('div', { class: 'card mt-4' });
  credCard.append(
    fromHtml(`<div class="card-head"><div class="card-title">设备密钥（设备凭证）</div></div>`),
  );
  const credBody = h('div', { class: 'p-4' });
  const creds = Array.isArray(data.credentials) ? data.credentials : [];

  if (!creds.length) {
    credBody.append(
      fromHtml(
        emptyState({
          icon: 'key',
          title: '尚未签发设备密钥',
          desc: '设备端上报心跳时需要用 DEVICE_SECRET 校验身份。签发后明文只显示一次，请立即复制给设备端。',
        }),
      ),
    );
  } else {
    credBody.append(
      fromHtml(
        table({
          columns: [
            {
              key: 'credential_type',
              title: '类型',
              width: '140px',
              render: (row) => html`<span class="mono text-xs">${row.credentialType || '-'}</span>`,
            },
            {
              key: 'secret_hint',
              title: '密钥（掩码）',
              render: (row) =>
                row.secretHint
                  ? html`<span class="mono text-xs">${row.secretHint}</span>`
                  : tag('无掩码', 'default'),
            },
            {
              key: 'algorithm',
              title: '摘要算法',
              width: '100px',
              render: (row) => esc(row.algorithm || '-'),
            },
            {
              key: 'issued_at',
              title: '签发时间',
              width: '150px',
              render: (row) => esc(showTime(row.issuedAt) || '-'),
            },
            {
              key: 'expires_at',
              title: '过期时间',
              width: '150px',
              render: (row) => esc(row.expiresAt ? formatDate(row.expiresAt) : '长期有效'),
            },
            {
              key: 'last_used_at',
              title: '最近使用',
              width: '150px',
              render: (row) => esc(row.lastUsedAt ? formatDate(row.lastUsedAt) : '-'),
            },
            {
              key: 'revoked_at',
              title: '吊销时间',
              width: '150px',
              render: (row) =>
                row.revokedAt
                  ? html`<span class="text-danger">${formatDate(row.revokedAt)}</span>`
                  : tag('有效', 'success'),
            },
          ],
          rows: creds,
          emptyText: '暂无凭证',
        }),
      ),
    );
  }

  if (auth.hasPerm(PERM.platform.deviceWrite) && data.assetStatus !== 'RETIRED') {
    const bar = h('div', { class: 'btn-group mt-3' });
    const issueBtn = h('button', {
      class: 'btn btn-primary',
      type: 'button',
      text: creds.length ? '重新签发设备密钥' : '签发设备密钥',
    });
    issueBtn.addEventListener('click', () => onIssueCredential?.());
    bar.append(issueBtn);
    credBody.append(bar);
  }
  credBody.append(
    fromHtml(html`<div class="field-hint mt-2">
      重新签发会立刻覆盖旧密钥（设备端必须同步更新，否则心跳会被拒绝）。
      页面只展示前 4 位掩码，任何接口都不会再返回明文。
    </div>`),
  );
  credCard.append(credBody);
  host.append(credCard);

  /* ---- 概览区底部的刷新入口（与页头「刷新」同一动作） ---- */
  const refreshBar = h('div', { class: 'mt-4' });
  const refreshBtn = h('button', { class: 'btn btn-sm', type: 'button', text: '刷新本页数据' });
  refreshBtn.addEventListener('click', () => reload?.());
  refreshBar.append(refreshBtn);
  host.append(refreshBar);
}

/* ------------------------------------------------------------
   四、绑定
   ------------------------------------------------------------ */

function renderBinding(host, data, ctx) {
  const { router } = ctx;
  const binding = data.binding;

  if (!binding) {
    host.append(
      fromHtml(
        emptyState({
          icon: 'link',
          title: '尚未绑定',
          desc: '这台设备还没有绑定记录。绑定动作由**商户端**发起（扫码），平台端不直接执行绑定。',
        }),
      ),
    );
    host.append(
      fromHtml(
        alert({
          tone: 'neutral',
          title: '怎么完成绑定',
          text:
            '① 平台侧先把设备分配到该租户（分配单执行成功）；' +
            '② 商户登录商户端，在「我的设备」里选择该设备并点击「绑定」；' +
            '③ 扫描设备机身 / 包装上的二维码，确认回显的 SN 与产品无误后提交。',
        }),
      ),
    );
    const jump = h('div', { class: 'mt-3' });
    const jumpBtn = h('button', { class: 'btn btn-sm', type: 'button', text: '返回设备库存' });
    jumpBtn.addEventListener('click', () => router.go('/devices'));
    jump.append(jumpBtn);
    host.append(jump);
    return;
  }

  host.append(
    fromHtml(
      statGrid([
        {
          label: '绑定状态',
          value: BINDING_RECORD_STATUS_MAP[binding.status]?.text || binding.status || '-',
          icon: 'link',
          tone: BINDING_RECORD_STATUS_MAP[binding.status]?.tone || 'default',
          foot: binding.statusLabel || '',
        },
        {
          label: '终端用户',
          value: binding.endUserId || '未填写',
          icon: 'user',
          tone: 'brand',
          foot: 'end_user_id（终端用户标识）',
        },
        {
          label: '绑定次数',
          value: binding.bindCount ?? 0,
          unit: '次',
          icon: 'refresh',
          tone: 'teal',
          foot: '解绑后重新绑定会累加',
        },
        {
          label: '二维码格式',
          value: binding.qrFormat || '-',
          icon: 'qrcode',
          tone: 'accent',
          foot: 'JX=集贤 4G / JD=京东 Wi-Fi',
        },
      ]),
    ),
  );

  const card = h('div', { class: 'card mt-4' });
  card.append(fromHtml(`<div class="card-head"><div class="card-title">绑定记录</div></div>`));
  card.append(
    fromHtml(
      descList([
        ['绑定记录 ID', binding.id],
        ['设备 SN', binding.sn || data.sn],
        ['设备标签', binding.deviceLabel],
        ['资产状态', statusText(binding.assetStatus)],
        ['绑定状态（设备维度）', statusText(binding.bindStatus)],
        ['联网方式', NETWORK_MAP[binding.networkType]?.text || binding.networkType],
        ['确认时间', showTime(binding.confirmedAt)],
        ['绑定时间', showTime(binding.boundAt)],
        ['绑定操作者', binding.boundBy],
        ['解绑时间', showTime(binding.unboundAt)],
        ['解绑操作者', binding.unboundBy],
        ['解绑原因', binding.unbindReason],
        ['备注', binding.remark],
        ['创建时间', showTime(binding.createdAt)],
        ['最后更新', showTime(binding.updatedAt)],
      ]),
    ),
  );
  host.append(card);

  host.append(
    fromHtml(
      alert({
        tone: 'info',
        title: '绑定状态说明',
        text:
          '待确认：扫码预检已签发一次性确认令牌，但商户还没点「确认绑定」（令牌 5 分钟过期，过期后记录会作废）；' +
          '已绑定：绑定已落库；已解绑：商户已填写原因解绑，可再次扫码重新绑定。',
      }),
    ),
  );
}

/* ------------------------------------------------------------
   五、事件时间线（分页：首屏 1 页，之后「加载更多」追加）
   ------------------------------------------------------------ */

function renderEvents(host, data) {
  host.append(fromHtml(loadingState('正在读取设备事件…')));

  const listHost = h('div');
  const footHost = h('div', { class: 'mt-3 text-center' });
  const items = [];
  let total = 0;
  let lastPage = 0;
  let mounted = false;

  /** 事件 → timeline 条目（TypeScript 风格的映射集中在这里，便于对照后端字段） */
  const toTimelineItem = (event) => {
    const typeLabel = EVENT_TYPE_LABELS[event.eventType] || event.eventType || '事件';
    const dimLabel = DIMENSION_LABELS[event.dimension] || '';
    const from = statusText(event.fromStatus);
    const to = statusText(event.toStatus);
    const move = from || to ? `${from || '—'} → ${to || '—'}` : '';

    const lines = [];
    if (event.summary) lines.push(event.summary);
    if (move) lines.push(`状态迁移：${move}`);
    if (event.actorAccount) lines.push(`操作者：${event.actorAccount}`);
    if (event.traceId) lines.push(`traceId：${event.traceId}`);

    return {
      title: dimLabel ? `${typeLabel}（${dimLabel}维度）` : typeLabel,
      time: formatRelative(event.createdAt),
      // ★ timeline() 的 body 契约是**纯文本**（组件内部用 html`` 转义后再插入），
      // 因此这里必须传字符串、由组件统一转义。早期实现传了 HTML 片段，
      // 结果被组件二次转义，页面上直接显示出 `<div>…</div>` 字面标签。
      // 多行靠 .timeline-body 的 white-space: pre-line 呈现。
      // 传纯文本同时也是最安全的：lines 里含用户填写的 summary（如解绑原因），
      // 由组件转义可以确保它永远进不了 DOM 结构。
      body: lines.join('\n'),
      tone: DIMENSION_TONES[event.dimension] || 'brand',
    };
  };

  const paint = () => {
    if (!items.length) {
      listHost.replaceChildren(
        fromHtml(
          emptyState({
            icon: 'activity',
            title: '暂无事件记录',
            desc: '设备的生成、入库存、分配、绑定、心跳等流转都会记录为事件。',
          }),
        ),
      );
      footHost.replaceChildren();
      return;
    }

    listHost.replaceChildren(fromHtml(timeline(items)));

    if (items.length < total) {
      const more = h('button', {
        class: 'btn btn-sm',
        type: 'button',
        text: `加载更多（已显示 ${items.length} / ${total} 条）`,
      });
      more.addEventListener('click', () => loadPage(lastPage + 1));
      footHost.replaceChildren(more);
    } else {
      footHost.replaceChildren(
        h('div', { class: 'text-xs text-secondary', text: `已显示全部 ${total} 条事件` }),
      );
    }
  };

  const loadPage = async (targetPage) => {
    try {
      const payload = await api.get(`/platform/devices/${data.id}/events`, {
        params: { page: targetPage, pageSize: EVENT_PAGE_SIZE },
      });
      lastPage = targetPage;
      total = Number(payload?.total ?? 0);
      const records = Array.isArray(payload?.records) ? payload.records : [];
      records.forEach((event) => items.push(toTimelineItem(event)));
      if (!mounted) {
        host.replaceChildren(listHost, footHost);
        mounted = true;
      }
      paint();
    } catch (error) {
      notifyDeviceError(error, '设备事件加载失败');
    }
  };

  loadPage(1);
}

/* ------------------------------------------------------------
   六、页面入口
   ------------------------------------------------------------ */

const TABS = [
  { key: 'overview', label: '概览' },
  { key: 'binding', label: '绑定' },
  { key: 'events', label: '事件时间线' },
];

/**
 * 按**设备当前状态**构建页头动作。
 *
 * 抽成函数的原因与订单详情页一致：动作是状态相关的（已冻结才有解冻、
 * 已报废不该再出现冻结与模拟心跳）。若只在首屏算一次，页面内完成
 * 冻结 / 解冻后按钮就过期了，用户必须手动 F5 才知道下一步能做什么。
 */
function buildActions(device) {
  const write = PERM.platform.deviceWrite;
  const list = [{ label: '刷新', icon: 'refresh', variant: 'ghost', action: 'reload-detail' }];

  if (device.assetStatus !== 'RETIRED') {
    list.push({
      label: '模拟心跳',
      icon: 'activity',
      variant: 'ghost',
      action: 'simulate-heartbeat',
      perm: write,
    });
    list.push({
      label: '签发设备密钥',
      icon: 'key',
      variant: 'ghost',
      action: 'issue-credential',
      perm: write,
    });
  }
  if (FREEZABLE.has(device.assetStatus)) {
    list.push({ label: '冻结', variant: 'ghost', action: 'freeze-device', perm: write });
  }
  if (device.assetStatus === 'FROZEN') {
    list.push({ label: '解冻', variant: 'primary', action: 'thaw-device', perm: write });
  }
  if (device.assetStatus !== 'RETIRED') {
    list.push({ label: '报废', variant: 'ghost', action: 'retire-device', perm: write });
  }
  return list;
}

/**
 * 渲染设备详情页。
 *
 * @param {HTMLElement} container
 * @param {{router: object, params: {id: string}}} ctx
 */
export async function renderDeviceDetail(container, ctx) {
  const { router, params } = ctx;
  const deviceId = params?.id;
  if (!deviceId) {
    router.go('/devices');
    return;
  }

  let data;
  try {
    data = await loadDevice(deviceId);
  } catch (error) {
    notifyDeviceError(error, '设备不存在或已被删除');
    router.go('/devices');
    return;
  }

  const detail = createDetailPage(container, {
    title: data.sn || `设备 ${deviceId}`,
    desc: `归属 ${data.tenantName || '平台自有库存'}　·　${
      data.clientProductName || '未关联客户产品'
    }　·　${NETWORK_MAP[data.networkType]?.text || data.networkType || '-'}`,
    backPath: '/devices',
    router,
    actions: buildActions(data),
    tabs: TABS,
    activeTab: 'overview',
  });

  /* ---- 页头标题旁的派生状态标签 ----
     createDetailPage 的 title 只接受字符串（会被转义），而这里要展示的是
     一个带色标签，因此把标签节点插到 h2 之后。reload() 会重绘它，
     否则冻结 / 解冻后标签会停留在旧状态。 */
  const labelHost = h('span', { class: 'ml-2' });
  detail.head.querySelector('.page-head-title h2')?.insertAdjacentElement('afterend', labelHost);

  const paintLabel = () => {
    labelHost.replaceChildren(
      fromHtml(tag(data.label || '-', ASSET_STATUS_MAP[data.assetStatus]?.tone || 'default')),
    );
  };
  paintLabel();

  const reload = async () => {
    try {
      data = await loadDevice(deviceId);
      // 状态可能已变化，页头动作与标签必须跟着变
      detail.setActions(buildActions(data));
      paintLabel();
      renderTab(detail.activeTab);
    } catch (error) {
      notifyDeviceError(error);
    }
  };

  const renderTab = (key) => {
    const host = h('div');
    detail.body.replaceChildren(host);

    if (key === 'binding') {
      renderBinding(host, data, { router });
      return;
    }
    if (key === 'events') {
      renderEvents(host, data);
      return;
    }
    renderOverview(host, data, {
      reload,
      onIssueCredential: () => issueCredential(data, reload),
    });
  };

  renderTab('overview');
  container.addEventListener('tabchange', (event) => renderTab(event.detail.tab));

  /* ---- 页头按钮 ----
     绑在 detail.head（本次渲染新建）而不是 container：
     container 由应用壳长期持有，绑在上面会随切页叠加监听器。 */
  detail.head.addEventListener('click', async (event) => {
    const target = event.target.closest('[data-action]');
    if (!target) return;
    const { action } = target.dataset;

    if (action === 'reload-detail') {
      await reload();
      toast.info('已刷新');
      return;
    }

    if (action === 'simulate-heartbeat') {
      await simulateHeartbeat(data, reload);
      return;
    }

    if (action === 'issue-credential') {
      await issueCredential(data, reload);
      return;
    }

    if (action === 'freeze-device') {
      await runWithReason({
        title: `冻结设备 ${data.sn}`,
        description: '冻结后该设备不可被分配、绑定或激活，用于处理异常与纠纷。',
        detail: '冻结会记录原因与操作者；解冻后恢复冻结前的资产状态。',
        confirmText: '确认冻结',
        reasonLabel: '冻结原因',
        run: (reason) =>
          api.post(
            `/platform/devices/${data.id}/freeze`,
            { reason },
            { idempotencyKey: api.newIdempotencyKey() },
          ),
        successMessage: '设备已冻结',
        onDone: reload,
      });
      return;
    }

    if (action === 'thaw-device') {
      await runWithReason({
        title: `解冻设备 ${data.sn}`,
        description: '解冻后设备恢复到冻结前的资产状态。',
        detail: '后端会校验当前是否为冻结态，若不是会拒绝并说明原因。',
        confirmText: '确认解冻',
        reasonLabel: '解冻原因',
        run: (reason) =>
          api.post(
            `/platform/devices/${data.id}/thaw`,
            { reason },
            { idempotencyKey: api.newIdempotencyKey() },
          ),
        successMessage: '设备已解冻',
        onDone: reload,
      });
      return;
    }

    if (action === 'retire-device') {
      await runWithReason({
        title: `报废设备 ${data.sn}`,
        description: '报废是不可逆操作，设备将进入终态，不能再分配或绑定。',
        detail: '请确认该设备已停止使用；报废会记录原因与操作者，供审计追溯。',
        tone: 'danger',
        confirmText: '确认报废',
        reasonLabel: '报废原因',
        run: (reason) =>
          api.post(
            `/platform/devices/${data.id}/retire`,
            { reason },
            { idempotencyKey: api.newIdempotencyKey() },
          ),
        successMessage: '设备已报废',
        onDone: reload,
      });
    }
  });
}

export default { renderDeviceDetail };
