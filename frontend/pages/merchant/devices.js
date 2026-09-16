/* ============================================================
   商户端 · 我的设备
   ------------------------------------------------------------
   对应后端 `/merchant/devices`：
     GET   列表（keyword / 四维状态 / clientProductId；服务端强制只返回本租户）
     GET   /{id}          设备详情（只读，用于本页的详情弹窗）
     POST  /bind/precheck 扫码预检（返回一次性确认令牌）
     POST  /{id}/bind     确认绑定（带 Idempotency-Key）
     POST  /{id}/unbind   解绑（原因必填）

   扫码绑定为什么必须两步
   ----------------------
   扫码结果不可信（用户可能扫了隔壁那台的码），precheck 先回显
   「你扫到的是 SN-xxx / 产品名 / 当前状态」让人确认，再签发一张
   5 分钟过期、用一次即销毁的确认令牌。因此本页的绑定弹窗里，
   「确认绑定」按钮在预检成功前**不出现**，并把令牌剩余秒数做成倒计时：
   过期后按钮禁用并提示重新扫码，而不是让用户点了收到 400。

   详情为什么是弹窗而不是跳页
   --------------------------
   本阶段商户端的路由清单只有 `/devices` 与 `/bindings`，**没有**设备详情页。
   与其跳到不存在的路由（白屏），不如就地用详情接口展示；
   代码仍保留了「若将来注册了详情路由则优先跳转」的分支。
   ============================================================ */

import api from '/shared/core/api.js';
import auth, { PERM } from '/shared/core/auth.js';
import { esc, formatDate, formatRelative, fromHtml, h, html, raw } from '/shared/ui/dom.js';
import {
  alert,
  descList,
  emptyState,
  loadingState,
  statGrid,
  statusTag,
  tag,
} from '/shared/ui/components.js';
import { createListPage } from '/shared/app/page.js';
import { confirmDialog, modal } from '/shared/ui/modal.js';
import { countSafe } from '/shared/app/dashboard.js';
import toast from '/shared/ui/toast.js';
import {
  ACTIVATION_STATUS_MAP,
  ACTIVATION_STATUS_OPTIONS,
  ASSET_STATUS_MAP,
  ASSET_STATUS_OPTIONS,
  BIND_STATUS_MAP,
  BIND_STATUS_OPTIONS,
  BINDING_RECORD_STATUS_MAP,
  NETWORK_MAP,
  ONLINE_STATUS_MAP,
  ONLINE_STATUS_OPTIONS,
  P5_ERROR_HINTS,
  fetchRecords,
  notifyError,
  toOptions,
} from '/pages/platform/common.js';

/** P5 错误码优先给出动作建议，其余交给既有提示表 */
function notifyP5Error(error, fallback = '操作失败，请稍后重试') {
  const hint = P5_ERROR_HINTS[error?.code];
  if (hint) {
    toast.error(`${error?.message || fallback}　${hint}`, { traceId: error?.traceId || '' });
    return;
  }
  notifyError(error, fallback);
}

/** 展示时间（空串由 descList 统一显示为 '-'） */
function showTime(value) {
  return value ? formatDate(value) : '';
}

/* ------------------------------------------------------------
   一、设备详情弹窗（只读）
   ------------------------------------------------------------ */

async function openDeviceDetail(deviceId) {
  const body = h('div');
  body.append(fromHtml(loadingState('正在读取设备详情…')));
  const dialog = modal({ title: '设备详情', size: 'lg', body });

  try {
    const data = await api.get(`/merchant/devices/${deviceId}`);
    const online = Boolean(data.online);

    body.replaceChildren();
    body.append(
      fromHtml(
        statGrid([
          {
            label: '资产状态',
            value: ASSET_STATUS_MAP[data.assetStatus]?.text || data.assetStatus || '-',
            icon: 'device',
            tone: ASSET_STATUS_MAP[data.assetStatus]?.tone || 'brand',
            foot: '设备本身的生命周期',
          },
          {
            label: '激活状态',
            value:
              ACTIVATION_STATUS_MAP[data.activationStatus]?.text || data.activationStatus || '-',
            icon: 'checkCircle',
            tone: ACTIVATION_STATUS_MAP[data.activationStatus]?.tone || 'default',
            foot: '终端用户是否已激活',
          },
          {
            label: '在线',
            value: online ? '在线' : '离线',
            icon: 'activity',
            tone: online ? 'success' : 'warning',
            foot: '180 秒心跳窗口内（以 online 为准）',
          },
          {
            label: '绑定状态',
            value: BIND_STATUS_MAP[data.bindStatus]?.text || data.bindStatus || '-',
            icon: 'link',
            tone: BIND_STATUS_MAP[data.bindStatus]?.tone || 'default',
            foot: '是否已绑定终端用户',
          },
        ]),
      ),
    );

    const card = h('div', { class: 'card mt-4' });
    card.append(fromHtml(`<div class="card-head"><div class="card-title">设备信息</div></div>`));
    card.append(
      fromHtml(
        descList([
          ['SN', data.sn],
          ['IMEI', data.imei],
          ['ICCID', data.iccid],
          ['MAC', data.mac],
          ['归属租户', data.tenantName],
          ['客户产品', data.clientProductName],
          ['订单号', data.orderNo],
          ['联网方式', NETWORK_MAP[data.networkType]?.text || data.networkType],
          ['固件版本', data.firmwareVersion],
          ['派生状态', data.label],
          ['最近心跳', data.lastHeartbeatAt ? formatRelative(data.lastHeartbeatAt) : ''],
          ['生成时间', showTime(data.generatedAt)],
          ['激活时间', showTime(data.activatedAt)],
          ['绑定时间', showTime(data.boundAt)],
          ['冻结原因', data.freezeReason],
          ['报废原因', data.retireReason],
          ['备注', data.remark],
        ]),
      ),
    );
    body.append(card);

    /* 当前绑定记录（详情接口直接带 binding，省一次往返） */
    const bindCard = h('div', { class: 'card mt-4' });
    bindCard.append(fromHtml(`<div class="card-head"><div class="card-title">当前绑定</div></div>`));
    const binding = data.binding;
    bindCard.append(
      fromHtml(
        binding
          ? descList([
              ['绑定状态', BINDING_RECORD_STATUS_MAP[binding.status]?.text || binding.status],
              ['终端用户', binding.endUserId],
              ['绑定次数', binding.bindCount ?? 0],
              ['绑定时间', showTime(binding.boundAt)],
              ['绑定操作者', binding.boundBy],
              ['解绑时间', showTime(binding.unboundAt)],
              ['解绑原因', binding.unbindReason],
              ['备注', binding.remark],
            ])
          : emptyState({
              icon: 'link',
              title: '尚未绑定',
              desc: '设备分配到本租户后，可在列表里点「绑定」并扫描设备二维码完成绑定。',
            }),
      ),
    );
    body.append(bindCard);
  } catch (error) {
    body.replaceChildren(
      fromHtml(
        emptyState({
          icon: 'alertTriangle',
          title: '设备详情读取失败',
          desc: error?.message || '请稍后重试',
        }),
      ),
    );
  }

  return dialog.result;
}

/* ------------------------------------------------------------
   二、扫码绑定弹窗（两步：预检 → 确认）
   ------------------------------------------------------------ */

/**
 * 打开扫码绑定弹窗。
 *
 * @param {object} device 列表行（提供 id / sn）
 * @param {Function} [onDone] 绑定成功后的回调（刷新列表）
 */
async function openBindDialog(device, onDone) {
  /** 预检结果：确认令牌**只放在内存里**，不写 URL、不写 localStorage */
  let precheck = null;
  let remaining = 0;
  let timer = null;

  const body = h('div');

  const qrInput = h('textarea', {
    class: 'textarea',
    rows: '3',
    placeholder: '把设备二维码的内容粘贴到这里（JX|… 或 JD|…）',
  });
  const precheckBtn = h('button', { class: 'btn btn-primary', type: 'button', text: '预检二维码' });

  const inputField = h('div', { class: 'field' });
  inputField.append(
    h('label', { class: 'field-label', text: '二维码内容' }),
    qrInput,
    h('div', {
      class: 'field-hint',
      text: '用扫码枪或手机扫码后把内容粘贴进来即可；二维码由平台在设备生成时签发。',
    }),
  );

  const precheckRow = h('div', { class: 'btn-group mt-2' });
  precheckRow.append(precheckBtn);

  const resultHost = h('div', { class: 'mt-3' });
  body.append(inputField, precheckRow, resultHost);

  /* 确认区（预检成功后才渲染） */
  const confirmArea = h('div', { class: 'mt-4' });
  const countdownEl = h('div', { class: 'field-hint' });
  const endUserInput = h('input', {
    class: 'input',
    placeholder: '终端用户标识，可留空',
  });
  const endUserField = h('div', { class: 'field' });
  endUserField.append(
    h('label', { class: 'field-label', text: '终端用户标识' }),
    endUserInput,
    h('div', { class: 'field-hint', text: '终端用户标识，可留空；P9 的终端用户模块落地后会做校验。' }),
  );

  const confirmBtn = h('button', { class: 'btn btn-primary', type: 'button', text: '确认绑定' });
  confirmBtn.disabled = true;
  const confirmBar = h('div', { class: 'btn-group mt-3' });
  confirmBar.append(confirmBtn);

  const stopTimer = () => {
    if (timer) {
      clearInterval(timer);
      timer = null;
    }
  };

  const paintCountdown = () => {
    if (!precheck) {
      countdownEl.textContent = '';
      confirmBtn.disabled = true;
      return;
    }
    if (remaining <= 0) {
      countdownEl.textContent = '确认码已过期，请重新扫码后再试。';
      countdownEl.classList.add('text-danger');
      confirmBtn.disabled = true;
      return;
    }
    countdownEl.classList.remove('text-danger');
    countdownEl.textContent = `确认码剩余有效时间：${remaining} 秒（过期后需重新扫码）`;
    confirmBtn.disabled = false;
  };

  const startCountdown = () => {
    stopTimer();
    paintCountdown();
    timer = setInterval(() => {
      remaining -= 1;
      paintCountdown();
      if (remaining <= 0) stopTimer();
    }, 1000);
  };

  /** 渲染预检回显：让用户确认「扫到的是哪一台」——两步流程的意义就在这里 */
  const paintPrecheck = () => {
    resultHost.replaceChildren();
    if (!precheck) return;

    resultHost.append(
      fromHtml(
        alert({
          tone: 'info',
          title: '请核对扫码结果',
          text: '确认下方 SN 与产品就是你要绑定的那台设备，再点「确认绑定」。',
        }),
      ),
    );
    resultHost.append(
      fromHtml(
        descList(
          [
            ['设备 SN', precheck.sn],
            ['设备标签', precheck.deviceLabel],
            ['客户产品', precheck.clientProductName],
            ['当前资产状态', ASSET_STATUS_MAP[precheck.assetStatus]?.text || precheck.assetStatus],
            ['联网方式', NETWORK_MAP[precheck.networkType]?.text || precheck.networkType],
            ['归属租户', precheck.tenantName],
            ['二维码格式', precheck.qrFormat],
          ],
          { cols: 2 },
        ),
      ),
    );

    confirmArea.replaceChildren(countdownEl, endUserField, confirmBar);
    if (!body.contains(confirmArea)) body.append(confirmArea);
    paintCountdown();
  };

  precheckBtn.addEventListener('click', async () => {
    const qrPayload = qrInput.value.trim();
    if (!qrPayload) {
      toast.warning('请先粘贴二维码内容');
      qrInput.focus();
      return;
    }

    precheckBtn.disabled = true;
    precheckBtn.textContent = '预检中…';
    try {
      const result = await api.post('/merchant/devices/bind/precheck', { qrPayload });
      precheck = result;
      remaining = Number(result?.expiresInSeconds ?? 300);
      paintPrecheck();
      startCountdown();
      toast.success(`已识别设备 ${result?.sn || ''}，请核对后确认绑定`);
    } catch (error) {
      precheck = null;
      stopTimer();
      resultHost.replaceChildren();
      if (body.contains(confirmArea)) confirmArea.remove();
      // 二维码无效 / 过期 / 设备已绑定等，必须把后端 message 展示出来
      notifyP5Error(error, '二维码预检失败');
    } finally {
      precheckBtn.disabled = false;
      precheckBtn.textContent = '预检二维码';
    }
  });

  confirmBtn.addEventListener('click', async () => {
    if (!precheck) return;
    if (remaining <= 0) {
      toast.warning('确认码已过期，请重新扫码');
      return;
    }

    confirmBtn.disabled = true;
    confirmBtn.textContent = '绑定中…';
    try {
      await api.post(
        `/merchant/devices/${device.id}/bind`,
        {
          confirmToken: precheck.confirmToken,
          endUserId: endUserInput.value.trim() || null,
          remark: null,
        },
        { idempotencyKey: api.newIdempotencyKey() },
      );
      // 令牌用一次即销毁：成功后立刻丢弃，避免被误复用
      precheck = null;
      stopTimer();
      toast.success(`设备 ${device.sn || ''} 已绑定`);
      dialog.close(true);
    } catch (error) {
      confirmBtn.disabled = false;
      confirmBtn.textContent = '确认绑定';
      // 过期 / 已绑定 / 不属于本租户等都要让用户看到原因
      notifyP5Error(error, '绑定失败');
    }
  });

  const dialog = modal({
    title: `扫码绑定设备 ${device.sn || device.id}`,
    subtitle: '两步完成：先预检回显，再确认绑定（确认码 5 分钟内有效）',
    size: 'lg',
    body,
    footer: (dlg) => {
      const cancel = h('button', { class: 'btn', type: 'button', text: '取消' });
      cancel.addEventListener('click', () => dlg.close(false));
      return [cancel];
    },
  });

  // 关闭弹窗时必须清掉倒计时，否则定时器会一直跑下去
  dialog.result.then(() => stopTimer());

  const done = await dialog.result;
  if (done) onDone?.();
  return done;
}

/* ------------------------------------------------------------
   三、列表
   ------------------------------------------------------------ */

/** 四维状态小标签（ADR-03：四个维度分列，便于定位「哪一维不对」） */
function fourDimTags(row) {
  return html`${raw(
    [
      statusTag(row.assetStatus, ASSET_STATUS_MAP),
      statusTag(row.activationStatus, ACTIVATION_STATUS_MAP),
      statusTag(row.onlineStatus, ONLINE_STATUS_MAP),
      statusTag(row.bindStatus, BIND_STATUS_MAP),
    ].join(''),
  )}`;
}

/** 表格列定义（产品名称靠下拉数据映射，故用工厂函数生成） */
function buildColumns({ productLabels }) {
  return [
    {
      key: 'sn',
      title: 'SN / IMEI / MAC',
      sortable: true,
      render: (row) => html`<div class="cell-stack">
      <span class="cell-strong mono">${row.sn || '-'}</span>
      <span class="cell-sub mono">${row.imei || '-'}</span>
      <span class="cell-sub mono">${row.mac || '-'}</span>
    </div>`,
    },
    {
      key: 'client_product_name',
      title: '客户产品',
      render: (row) => {
        if (!row.clientProductId) return '-';
        const known = productLabels.get(String(row.clientProductId));
        return html`<div class="cell-stack">
          <span>${row.clientProductName || known?.name || '-'}</span>
          <span class="cell-sub mono">${row.clientProductCode || known?.code || row.clientProductId}</span>
        </div>`;
      },
    },
    {
      key: 'label',
      title: '状态',
      width: '120px',
      render: (row) => tag(row.label || '-', ASSET_STATUS_MAP[row.assetStatus]?.tone || 'default'),
    },
    {
      key: 'four_dim',
      title: '四维（资产 / 激活 / 在线 / 绑定）',
      render: (row) => fourDimTags(row),
    },
    {
      key: 'network_type',
      title: '联网方式',
      width: '105px',
      render: (row) => statusTag(row.networkType, NETWORK_MAP),
    },
    {
      key: 'online',
      title: '在线',
      width: '90px',
      // 与平台端同口径：展示以布尔 online 为准，onlineStatus 只用于筛选
      render: (row) => tag(row.online ? '在线' : '离线', row.online ? 'success' : 'warning'),
    },
    {
      key: 'generated_at',
      title: '生成时间',
      sortable: true,
      width: '150px',
      render: (row) => esc(formatDate(row.generatedAt || row.createdAt)),
    },
    {
      key: 'actions',
      title: '操作',
      align: 'right',
      width: '200px',
      nowrap: true,
      render: (row) => {
        const canBind = auth.hasPerm(PERM.merchant.bindingWrite);
        const buttons = [
          `<button type="button" class="btn btn-sm btn-ghost" data-action="open-device" data-id="${esc(row.id)}">详情</button>`,
        ];
        // 绑定入口只在「已分配且未绑定」时出现：设备还没到本租户手上时，
        // 扫码也只会得到一个 DEVICE_NOT_IN_TENANT / NOT_AVAILABLE
        if (canBind && row.assetStatus === 'ALLOCATED' && row.bindStatus === 'UNBOUND') {
          buttons.push(
            `<button type="button" class="btn btn-sm btn-primary" data-action="bind-device" data-id="${esc(row.id)}">绑定</button>`,
          );
        }
        if (canBind && row.bindStatus === 'BOUND') {
          buttons.push(
            `<button type="button" class="btn btn-sm btn-danger" data-action="unbind-device" data-id="${esc(row.id)}">解绑</button>`,
          );
        }
        return html`<div class="btn-group">${raw(buttons.join(''))}</div>`;
      },
    },
  ];
}

/** 解绑（原因必填，与平台端冻结 / 报废同一口径） */
async function unbindDevice(device, reload) {
  const { confirmed, reason } = await confirmDialog({
    title: `解绑设备 ${device.sn || device.id}`,
    description: '解绑后终端用户将无法继续使用该设备，可再次扫码重新绑定。',
    detail: '解绑原因会写入绑定记录与审计日志，用于后续的责任追溯。',
    tone: 'danger',
    confirmText: '确认解绑',
    requireReason: true,
    reasonLabel: '解绑原因',
  });
  if (!confirmed) return;

  try {
    await api.post(
      `/merchant/devices/${device.id}/unbind`,
      { reason },
      { idempotencyKey: api.newIdempotencyKey() },
    );
    toast.success('设备已解绑');
    reload();
  } catch (error) {
    notifyP5Error(error, '解绑失败');
  }
}

/**
 * 渲染「我的设备」页。
 *
 * @param {HTMLElement} container
 * @param {{router: object}} ctx
 */
export async function renderMerchantDevices(container, ctx) {
  const { router } = ctx;

  const products = await fetchRecords('/merchant/products');
  const productOptions = toOptions(products, (item) => `${item.name}（${item.code}）`);
  const productLabels = new Map(
    products.map((item) => [String(item.id), { name: item.name, code: item.code }]),
  );

  const page = createListPage({
    container,
    title: '我的设备',
    desc: '本租户名下的设备（服务端强制按租户隔离）。绑定需扫码完成，解绑需填写原因；「在线」以 180 秒心跳窗口为准。',
    actions: [{ label: '刷新', icon: 'refresh', action: 'reload-list' }],
    columns: buildColumns({ productLabels }),
    fetcher: (params) =>
      api.get('/merchant/devices', {
        params: {
          page: params.page,
          pageSize: params.pageSize,
          sortBy: params.sortBy,
          order: params.order,
          keyword: params.keyword,
          assetStatus: params.assetStatus,
          activationStatus: params.activationStatus,
          onlineStatus: params.onlineStatus,
          bindStatus: params.bindStatus,
          clientProductId: params.clientProductId,
        },
      }),
    filters: [
      { key: 'keyword', placeholder: 'SN / IMEI / MAC', width: '200px' },
      { key: 'assetStatus', type: 'select', options: ASSET_STATUS_OPTIONS },
      { key: 'activationStatus', type: 'select', options: ACTIVATION_STATUS_OPTIONS },
      { key: 'onlineStatus', type: 'select', options: ONLINE_STATUS_OPTIONS },
      { key: 'bindStatus', type: 'select', options: BIND_STATUS_OPTIONS },
      {
        key: 'clientProductId',
        type: 'select',
        options: [{ value: '', label: '全部产品' }, ...productOptions],
      },
    ],
    onReady: (host, instance) => {
      if (!instance.state.loading) refreshStats(instance.filters);
    },
    onAction: async (action, target, { table: tbl, reload }) => {
      const row = target.dataset.id ? tbl.findRowById(target.dataset.id) : null;

      if (action === 'reload-list') {
        tbl.load();
        return;
      }

      if (action === 'open-device') {
        if (!row) return;
        // 本阶段商户端没有设备详情路由；若将来注册了则优先跳转（与平台端同样的防御写法）
        const hasRoute = (router.routes || []).some((item) => item.name === 'merchantDeviceDetail');
        if (hasRoute) router.goByName('merchantDeviceDetail', { id: row.id });
        else await openDeviceDetail(row.id);
        return;
      }

      if (action === 'bind-device') {
        if (row) await openBindDialog(row, reload);
        return;
      }

      if (action === 'unbind-device') {
        if (row) await unbindDevice(row, reload);
      }
    },
  });

  /* ---- 统计卡：插在页头与表格之间（与平台端设备库存同一位置策略） ---- */
  const statsHost = h('div', { class: 'mb-4' });
  page.root.insertBefore(statsHost, page.root.children[1] || null);

  let latest = { total: '—', allocated: '—', bound: '—', online: '—' };

  const paintStats = () => {
    statsHost.replaceChildren(
      fromHtml(
        statGrid([
          {
            label: '设备总数',
            value: latest.total,
            unit: '台',
            icon: 'device',
            tone: 'brand',
            foot: '当前筛选条件下的设备',
          },
          {
            label: '已分配待激活',
            value: latest.allocated,
            unit: '台',
            icon: 'package',
            tone: 'teal',
            foot: '资产状态 = 已分配（已到本租户名下）',
          },
          {
            label: '已绑定',
            value: latest.bound,
            unit: '台',
            icon: 'link',
            tone: 'accent',
            foot: '绑定状态 = 已绑定',
          },
          {
            label: '在线',
            value: latest.online,
            unit: '台',
            icon: 'activity',
            tone: 'coral',
            foot: '180 秒心跳窗口内（与列表「在线」列同口径）',
          },
        ]),
      ),
    );
  };

  async function refreshStats(filters = {}) {
    const [total, allocated, bound, online] = await Promise.all([
      countSafe('/merchant/devices', { ...filters }),
      countSafe('/merchant/devices', { ...filters, assetStatus: 'ALLOCATED' }),
      countSafe('/merchant/devices', { ...filters, bindStatus: 'BOUND' }),
      countSafe('/merchant/devices', { ...filters, onlineStatus: 'ONLINE' }),
    ]);
    latest = { total, allocated, bound, online };
    paintStats();
  }

  paintStats();

  return page;
}

export default { renderMerchantDevices };
