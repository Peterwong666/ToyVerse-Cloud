/* ============================================================
   商户端 · AI 配置（P9 核心页面）
   ------------------------------------------------------------
   路由：`#/ai-config`（支持 `?productId=xxx` 预选产品）

   对应后端：
     GET  /merchant/products/{id}/ai-config          读取当前配置
     PUT  /merchant/products/{id}/ai-config          供应商
     PUT  /merchant/products/{id}/ai-config/prompt   提示词与采样参数
     PUT  /merchant/products/{id}/ai-config/role     角色预设（Wi-Fi 产品 409）
     PUT  /merchant/products/{id}/ai-config/voice    音色
     PUT  /merchant/products/{id}/ai-config/safety   内容安全三开关
     GET  /merchant/ai/providers                     供应商下拉
     GET  /merchant/role-presets                     角色预设下拉
     GET  /merchant/voice-profiles                   音色下拉

   为什么五个分区**各自独立保存**
   ------------------------------
   后端把供应商 / 提示词 / 角色 / 音色 / 安全拆成五个子端点，本页就照抄这个
   粒度：一个「保存全部」按钮会在某一区失败时把其它区的成功也变成不确定
   （用户不知道到底哪部分生效了）。分区保存的代价只是多点几次，
   换来的是「每次改动的影响面恰好是用户看到的那一区」。

   为什么角色分区要按 roleSupported 整个禁用
   ----------------------------------------
   Wi-Fi 方案（京东 JoyInside）不支持平台侧角色预设，后端对该端点直接
   返回 409。与其让用户点了才收到错误，不如一开始就禁用控件并展示
   `roleUnsupportedReason` —— 这也解释了「为什么这个产品改不了角色」。

   为什么用「跟随平台默认」而不是强制选一个供应商
   --------------------------------------------
   `AiConfig.provider_code` 为空是**有语义的**：按解析顺序继续下探
   （产品 → 模板厂商 → 平台默认）。把它当成「未填写」而强制必填，
   会把「刻意留空」变成「必须显式选一遍」，反而丢掉了下探能力。

   为什么温度滑块要归一化
   ----------------------
   后端把温度以「放大 100 倍的整数」落库（见 models/ai.py 的
   `AiConfig.temperature` 注释），对外契约是 0–2 的小数。两种口径都做
   容忍，避免滑块被撑到 200 而无法操作。
   ============================================================ */

import api from '/shared/core/api.js';
import auth, { PERM } from '/shared/core/auth.js';
import { clear, esc, formatRelative, fromHtml, h, html, raw } from '/shared/ui/dom.js';
import { alert, button, card, emptyState, loadingState, tag } from '/shared/ui/components.js';
import { renderPageHead } from '/shared/app/page.js';
import { collect, form } from '/shared/ui/form.js';
import toast from '/shared/ui/toast.js';
import { fetchRecords, notifyError } from '/pages/platform/common.js';

/** 供应商健康状态（取值同后端 ProviderHealth） */
const HEALTH_MAP = {
  UP: { text: '可用', tone: 'success' },
  DEGRADED: { text: '能力受限', tone: 'warning' },
  DOWN: { text: '不可用', tone: 'danger' },
};

/** 音色训练状态（取值同后端 VoiceTrainStatus） */
const TRAIN_MAP = {
  PENDING: '待训练',
  TRAINING: '训练中',
  SUCCESS: '训练完成',
  FAILED: '训练失败',
};

const SAFETY_FIELDS = [
  {
    key: 'enabled',
    label: '内容安全总开关',
    type: 'switch',
    span: 2,
    hint: '关闭后命中敏感词的内容不会被拦截，会原样下发出设备（不建议对儿童产品关闭）。',
  },
  {
    key: 'sensitiveWords',
    label: '敏感词过滤',
    type: 'switch',
    span: 2,
    hint: '命中敏感词时把整段回复替换为安全文案，原文不会下发出设备。',
  },
  {
    key: 'llmReview',
    label: '大模型审核',
    type: 'switch',
    span: 2,
    hint: '每次回复在下发前再经一次大模型审核；更稳妥，但会增加一点延迟。',
  },
];

/* ------------------------------------------------------------
   一、小工具
   ------------------------------------------------------------ */

/** 温度归一化：兼容「0–2 小数」与「放大 100 倍的整数」两种口径 */
function normTemp(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return 0;
  const normalized = num > 2 ? num / 100 : num;
  return Math.max(0, Math.min(2, Math.round(normalized * 10) / 10));
}

/** 供应商健康标签（未探测时显示「未知」，不假装可用） */
function healthTag(status) {
  const item = HEALTH_MAP[status];
  return item ? tag(item.text, item.tone) : tag(status || '未探测', 'default');
}

/**
 * 开关状态的展示标签。
 * @param {boolean} on
 * @param {string} [offTone] 关闭时的配色：总开关关闭用 danger，子项关闭用 warning
 */
function onOffTag(on, offTone = 'warning') {
  return on ? tag('开启', 'success') : tag('关闭', offTone);
}

/** 滑块自定义控件（form.js 的 custom 类型：需要自己保证控件 HTML 已转义） */
function temperatureControl(value) {
  const current = normTemp(value);
  return html`<div class="flex items-center gap-3">
    <input type="range" min="0" max="2" step="0.1" value="${current}"
           id="field-temperature" name="temperature" data-slider="temperature" style="flex:1" />
    <span class="num mono" data-slider-value="temperature" style="min-width:3.2em;text-align:right">
      ${current.toFixed(1)}
    </span>
  </div>`;
}

/** 把「当前生效说明」翻译成人话（这是给运营看的，不是给工程师看的） */
function effectLines(safety) {
  if (!safety.enabled) {
    return [
      '内容安全总开关已关闭：命中敏感词的内容会被直接下发出设备，不做任何拦截。',
      '面向儿童的产品不建议关闭；若只是为了联调，请改完立刻恢复。',
    ];
  }
  return [
    safety.sensitiveWords
      ? '命中敏感词时整段替换为安全文案，原文不会下发出设备。'
      : '敏感词过滤已关闭：命中敏感词的内容会原样下发出设备（仅剩大模型审核这一道防线）。',
    safety.llmReview
      ? '每次回复在下发到设备前额外经过一次大模型审核，更稳妥，但首字延迟会略增。'
      : '未开启大模型审核：只靠敏感词表拦截，词表未收录的表达不会被拦下。',
  ];
}

/**
 * 创建一个「分区卡片」：标题 + 说明 + 右上角保存按钮 + 表单。
 *
 * @param {object} o
 * @param {string} o.title
 * @param {string} [o.subtitle]
 * @param {Array} o.fields      form.js 字段定义
 * @param {object} o.values     初始值
 * @param {number} [o.columns]
 * @param {string} [o.saveAction] data-action 值（空则不渲染保存按钮）
 * @param {boolean}[o.canWrite]
 * @param {string} [o.extraHtml] 表单上方的提示 HTML（已生成、可信）
 * @returns {{cardEl: HTMLElement, formEl: HTMLElement, saveBtn: ?HTMLElement}}
 */
function mountSection(o) {
  const {
    title,
    subtitle = '',
    fields,
    values = {},
    columns = 1,
    saveAction = '',
    canWrite = true,
    extraHtml = '',
  } = o;

  const cardEl = fromHtml(
    card({
      title,
      subtitle,
      actions: saveAction
        ? button({ label: '保存', variant: 'primary', size: 'sm', action: saveAction, disabled: !canWrite })
        : '',
    }),
  );

  const bodyEl = h('div');
  if (extraHtml) bodyEl.append(fromHtml(extraHtml));

  const formEl = fromHtml(
    form({ fields, values, columns, id: `ai-form-${saveAction || 'section'}`.replace(/[^\w-]/g, '') }),
  );
  bodyEl.append(formEl);
  cardEl.append(bodyEl);

  return { cardEl, formEl, saveBtn: cardEl.querySelector(`[data-action="${saveAction}"]`) };
}

/* ------------------------------------------------------------
   二、页面
   ------------------------------------------------------------ */

/**
 * 渲染 AI 配置页。
 *
 * @param {HTMLElement} container
 * @param {{router: object, query: object}} ctx
 */
export async function renderAiConfig(container, ctx) {
  const { router, query } = ctx;
  const canWrite = auth.hasPerm(PERM.merchant.aiConfigWrite);

  clear(container);
  const root = h('div', { class: 'page-body' });
  container.append(root);

  renderPageHead(root, {
    title: 'AI 配置',
    desc:
      '按产品设置对话供应商、提示词、角色、音色与内容安全。每个分区独立保存：' +
      '保存成功后该分区的值即为设备实际使用的配置。',
  });

  /* ---- 产品下拉（无产品时直接给空态，不请求配置） ---- */
  const products = await fetchRecords('/merchant/products');
  if (!products.length) {
    const host = h('div');
    host.append(
      fromHtml(
        emptyState({
          icon: 'package',
          title: '暂无可配置的产品',
          desc: '本租户还没有客户产品。客户产品由平台侧创建并授权后再分配给你，请先联系平台运营。',
        }),
      ),
    );
    root.append(host);
    return;
  }

  const requested = query?.productId;
  let productId = products.some((item) => String(item.id) === String(requested))
    ? String(requested)
    : String(products[0].id);

  const selectEl = h('select', { class: 'select', style: { maxWidth: '380px' } });
  products.forEach((item) => {
    selectEl.append(h('option', { value: item.id, text: `${item.name}（${item.code}）` }));
  });
  selectEl.value = productId;

  const toolbar = h('div', { class: 'card mb-4' });
  toolbar.append(
    h(
      'div',
      { class: 'flex items-center gap-3 flex-wrap' },
      h('span', { class: 'text-secondary text-sm', text: '客户产品' }),
      selectEl,
    ),
    h('div', {
      class: 'field-hint mt-2',
      text:
        '选择产品后即显示该产品当前生效的配置。切换产品不会自动保存，未点保存的改动会丢失。',
    }),
  );
  root.append(toolbar);

  const configHost = h('div');
  const effectHost = h('div');
  root.append(configHost, effectHost);

  /* ---- 下拉数据：静默失败（取不到就退化为空下拉，不挡住页面） ---- */
  const [providers, rolePresets, voiceProfiles] = await Promise.all([
    fetchRecords('/merchant/ai/providers'),
    fetchRecords('/merchant/role-presets'),
    fetchRecords('/merchant/voice-profiles'),
  ]);

  /** 当前配置（每次保存后重新拉取） */
  let cfg = null;

  const providerOptions = [
    { value: '', label: '跟随平台默认（按 产品 → 模板 → 平台 顺序解析）' },
    ...providers.map((item) => ({
      value: item.code,
      label: `${item.name}（${item.code}）${item.configured === false ? ' · 未配置密钥' : ''}`,
    })),
  ];

  const roleOptions = [
    { value: '', label: '不指定角色' },
    ...rolePresets.map((item) => ({
      value: item.code,
      label: `${item.name}${item.ageGroup ? ` · ${item.ageGroup}` : ''}`,
    })),
  ];

  const voiceOptions = [
    { value: '', label: '不指定音色（使用供应商默认音色）' },
    ...voiceProfiles.map((item) => ({
      value: item.id,
      label: `${item.name}${item.language ? ` · ${item.language}` : ''}${
        item.trainStatus && item.trainStatus !== 'SUCCESS' ? ` · ${TRAIN_MAP[item.trainStatus] || item.trainStatus}` : ''
      }`,
    })),
  ];

  /**
   * 统一保存入口：成功 → toast + 重新拉取配置。
   *
   * 失败时必须把按钮恢复可用：保存失败后整个分区没有重渲染，
   * 按钮若停在 disabled，用户就再也点不动了（只能刷新页面）。
   *
   * @param {?HTMLElement} saveBtn 触发保存的按钮（用于 loading 与恢复）
   * @param {string} path
   * @param {object} body
   * @param {string} successMessage
   */
  async function save(saveBtn, path, body, successMessage) {
    if (!canWrite) {
      toast.warning('当前账号没有 AI 配置的写权限');
      return;
    }

    if (saveBtn) {
      saveBtn.disabled = true;
      saveBtn.classList.add('btn-loading');
    }

    try {
      await api.put(path, body, { idempotencyKey: api.newIdempotencyKey() });
      toast.success(successMessage);
      // 成功后整区重渲染，旧按钮节点被丢弃，无需恢复
      await loadConfig();
    } catch (error) {
      if (saveBtn) {
        saveBtn.disabled = false;
        saveBtn.classList.remove('btn-loading');
      }
      // 角色端点在「产品联网方案不支持」时返回 409，原因必须让用户看见
      if (error?.status === 409) {
        toast.error(
          `${error.message || '保存失败'}　该产品的联网方案可能不支持此项配置，请刷新后查看最新状态。`,
          { traceId: error?.traceId || '' },
        );
        return;
      }
      notifyError(error, '保存失败');
    }
  }

  /* ---- 各分区的渲染函数 ---- */

  function renderProvider() {
    const extra = cfg.providerConfigured === false
      ? alert({
          tone: 'danger',
          title: '该供应商尚未配置密钥',
          text:
            '平台上还没有为这个供应商录入密钥，选定后对话会按「安全失败」处理：' +
            '设备会收到明确错误，而不是伪造一条回复。请先到平台端完成供应商配置。',
        })
      : '';

    const { cardEl, formEl, saveBtn } = mountSection({
      title: '供应商',
      subtitle: '决定由哪个大模型服务产生回复；留空表示按解析顺序下探。',
      fields: [
        {
          key: 'providerCode',
          label: 'AI 供应商',
          type: 'select',
          options: providerOptions,
          hint: '下拉中标注「未配置密钥」的供应商选定后对话会安全失败。',
        },
      ],
      values: { providerCode: cfg.providerCode ?? '' },
      saveAction: 'save-provider',
      canWrite,
      extraHtml: extra,
    });

    // 当前生效的供应商与健康状态（只读回显，避免用户以为下拉就是现状）
    formEl.append(
      fromHtml(
        html`<div class="field-hint mt-2">
          当前生效：${cfg.providerName || cfg.providerCode || '未指定'}　·　
          密钥：${cfg.providerConfigured === false ? '未配置' : '已配置'}　·　
          健康：${raw(healthTag(cfg.providerHealth))}
        </div>`,
      ),
    );

    saveBtn?.addEventListener('click', () => {
      const values = collect(formEl, [{ key: 'providerCode' }]);
      save(
        saveBtn,
        `/merchant/products/${productId}/ai-config`,
        { providerCode: values.providerCode },
        '供应商已保存',
      );
    });

    return cardEl;
  }

  function renderPrompt() {
    const { cardEl, formEl, saveBtn } = mountSection({
      title: '提示词与采样参数',
      subtitle: '系统提示词决定助手的说话方式；温度越高越发散，越低越稳定。',
      columns: 2,
      fields: [
        {
          key: 'systemPrompt',
          label: '系统提示词',
          type: 'textarea',
          rows: 6,
          span: 2,
          maxLength: 8000,
          placeholder: '例如：你是一只会讲睡前故事的熊猫，用 3-6 岁孩子能听懂的话回答。',
          hint: '会作为对话的系统消息下发；与角色预设同时存在时以角色预设的人设为准。',
        },
        { key: 'greeting', label: '开场白', maxLength: 512, span: 2, placeholder: '设备开机 / 唤醒后说的第一句话' },
        {
          key: 'temperature',
          label: '温度（0–2）',
          type: 'custom',
          span: 1,
          control: temperatureControl(cfg.temperature),
          hint: '0 最稳定、2 最发散；儿童玩具建议 0.5–0.9。',
        },
        {
          key: 'maxTokens',
          label: '最大回复长度（tokens）',
          type: 'number',
          span: 1,
          min: 1,
          placeholder: '留空表示用供应商默认值',
          hint: '太长会让设备播报变慢，建议 200–400。',
        },
      ],
      values: {
        systemPrompt: cfg.systemPrompt ?? '',
        greeting: cfg.greeting ?? '',
        maxTokens: cfg.maxTokens ?? '',
      },
      saveAction: 'save-prompt',
      canWrite,
    });

    // 滑块与数字标签联动（form.js 的 custom 类型不做类型转换，需自行同步显示）
    const slider = formEl.querySelector('[data-slider="temperature"]');
    const sliderLabel = formEl.querySelector('[data-slider-value="temperature"]');
    slider?.addEventListener('input', () => {
      sliderLabel.textContent = Number(slider.value).toFixed(1);
    });

    saveBtn?.addEventListener('click', () => {
      const values = collect(formEl, [
        { key: 'systemPrompt' },
        { key: 'greeting' },
        { key: 'maxTokens', type: 'number' },
      ]);
      const temperature = slider ? Number(slider.value) : normTemp(cfg.temperature);
      save(
        saveBtn,
        `/merchant/products/${productId}/ai-config/prompt`,
        {
          systemPrompt: values.systemPrompt,
          greeting: values.greeting,
          temperature,
          maxTokens: values.maxTokens,
        },
        '提示词与采样参数已保存',
      );
    });

    return cardEl;
  }

  function renderRole() {
    const supported = cfg.roleSupported !== false;

    const extra = supported
      ? ''
      : alert({
          tone: 'warning',
          title: '该产品不支持平台侧角色预设',
          text:
            `后端未通过校验：${cfg.roleUnsupportedReason || '未返回具体原因'}。` +
            // 刻意**不点名具体厂商**：Wi-Fi 只是「对话由厂商智能体提供」这一类方案的统称
            // （京东 JoyInside、火山引擎都属此类）。写死厂商名会在换供应商后
            // 变成误导文案——与 OTA 的 `ota_support` 一样，规则应数据驱动、
            // 文案应只描述规则。
            'Wi-Fi 方案的对话角色由厂商侧的智能体配置决定，平台无法下发角色预设，' +
            '因此本分区整体禁用——不是页面故障。',
        });

    const { cardEl, formEl, saveBtn } = mountSection({
      title: '角色预设',
      subtitle: '决定助手的固定人设与开场白（平台内置或本租户自定义）。',
      fields: [
        {
          key: 'rolePresetCode',
          label: '角色预设',
          type: 'select',
          options: roleOptions,
          disabled: !supported,
          hint: supported ? '选定后设备按该人设对话。' : '当前产品不支持角色预设，下拉已禁用。',
        },
      ],
      values: { rolePresetCode: cfg.rolePresetCode ?? '' },
      saveAction: 'save-role',
      canWrite: canWrite && supported,
      extraHtml: extra,
    });

    formEl.append(
      fromHtml(
        html`<div class="field-hint mt-2">
          当前生效：${cfg.rolePresetName || cfg.rolePresetCode || '未指定'}　·　
          平台支持：${supported ? '支持' : '不支持'}
        </div>`,
      ),
    );

    saveBtn?.addEventListener('click', () => {
      const values = collect(formEl, [{ key: 'rolePresetCode' }]);
      save(
        saveBtn,
        `/merchant/products/${productId}/ai-config/role`,
        { rolePresetCode: values.rolePresetCode },
        '角色预设已保存',
      );
    });

    return cardEl;
  }

  function renderVoice() {
    const { cardEl, formEl, saveBtn } = mountSection({
      title: '音色',
      subtitle: '设备播报使用的声音；来自本租户的音色档案（含火山声音复刻训练结果）。',
      fields: [
        {
          key: 'voiceProfileId',
          label: '音色档案',
          type: 'select',
          options: voiceOptions,
          hint: voiceProfiles.length
            ? '训练未完成的音色仍可使用供应商默认音色兜底。'
            : '本租户还没有音色档案，当前只能使用供应商默认音色。',
        },
      ],
      values: { voiceProfileId: cfg.voiceProfileId ?? '' },
      saveAction: 'save-voice',
      canWrite,
    });

    formEl.append(
      fromHtml(
        html`<div class="field-hint mt-2">当前生效：${cfg.voiceProfileName || cfg.voiceProfileId || '供应商默认音色'}</div>`,
      ),
    );

    saveBtn?.addEventListener('click', () => {
      const values = collect(formEl, [{ key: 'voiceProfileId' }]);
      save(
        saveBtn,
        `/merchant/products/${productId}/ai-config/voice`,
        { voiceProfileId: values.voiceProfileId },
        '音色已保存',
      );
    });

    return cardEl;
  }

  /** 底部「当前生效说明」：把三个开关翻译成人话，并显示最后更新时间 */
  function paintEffect(formEl) {
    const safety = formEl
      ? collect(formEl, SAFETY_FIELDS)
      : {
          enabled: cfg.safety?.enabled !== false,
          sensitiveWords: cfg.safety?.sensitiveWords !== false,
          llmReview: Boolean(cfg.safety?.llmReview),
        };

    const state = {
      enabled: Boolean(safety.enabled),
      sensitiveWords: Boolean(safety.sensitiveWords),
      llmReview: Boolean(safety.llmReview),
    };

    const lines = effectLines(state)
      .map((line) => `<li>${esc(line)}</li>`)
      .join('');

    effectHost.replaceChildren(
      fromHtml(
        card({
          title: '当前生效说明',
          subtitle: '下面这段话就是设备实际的行为，不需要再去读接口文档。',
          body: html`
            <div class="mb-3">
              <span class="text-sm text-secondary">总开关</span>
              ${raw(onOffTag(state.enabled, 'danger'))}
              <span class="text-sm text-secondary" style="margin-left:12px">敏感词过滤</span>
              ${raw(onOffTag(state.sensitiveWords))}
              <span class="text-sm text-secondary" style="margin-left:12px">大模型审核</span>
              ${raw(onOffTag(state.llmReview))}
            </div>
            <ul class="text-body" style="margin:0;padding-left:1.2em;line-height:1.9">${raw(lines)}</ul>
            <div class="field-hint mt-3">配置最后更新：${formatRelative(cfg.updatedAt)}</div>
          `,
        }),
      ),
    );
  }

  function renderSafety() {
    const { cardEl, formEl, saveBtn } = mountSection({
      title: '内容安全',
      subtitle: '三道开关决定「什么内容能下发出设备」。保存后立即对本产品的下一次回复生效。',
      fields: SAFETY_FIELDS,
      values: {
        enabled: cfg.safety?.enabled !== false,
        sensitiveWords: cfg.safety?.sensitiveWords !== false,
        llmReview: Boolean(cfg.safety?.llmReview),
      },
      saveAction: 'save-safety',
      canWrite,
    });

    // 开关变化时即时刷新底部说明（不必等到保存，用户可以先看清后果再决定）
    formEl.addEventListener('change', () => paintEffect(formEl));

    saveBtn?.addEventListener('click', () => {
      const values = collect(formEl, SAFETY_FIELDS);
      save(
        saveBtn,
        `/merchant/products/${productId}/ai-config/safety`,
        {
          enabled: Boolean(values.enabled),
          sensitiveWords: Boolean(values.sensitiveWords),
          llmReview: Boolean(values.llmReview),
        },
        '内容安全设置已保存',
      );
    });

    return cardEl;
  }

  function paint() {
    configHost.replaceChildren();
    configHost.append(renderProvider(), renderPrompt(), renderRole(), renderVoice());

    const safetyCard = renderSafety();
    configHost.append(safetyCard);
    paintEffect(safetyCard.querySelector('form'));
  }

  async function loadConfig() {
    configHost.replaceChildren(fromHtml(loadingState('正在读取 AI 配置…')));
    effectHost.replaceChildren();
    try {
      cfg = await api.get(`/merchant/products/${productId}/ai-config`);
      paint();
    } catch (error) {
      notifyError(error, 'AI 配置读取失败');
      configHost.replaceChildren(
        fromHtml(
          emptyState({
            icon: 'alertTriangle',
            title: 'AI 配置读取失败',
            desc: error?.message || '请稍后重试。',
            action: '<button class="btn btn-primary mt-2" data-action="retry-config">重新加载</button>',
          }),
        ),
      );
    }
  }

  selectEl.addEventListener('change', () => {
    productId = selectEl.value;
    loadConfig();
  });

  // 重试按钮（空态里）用一次性委托，绑在本次渲染的 root 上
  root.addEventListener('click', (event) => {
    if (event.target.closest('[data-action="retry-config"]')) loadConfig();
  });

  await loadConfig();
}

export default { renderAiConfig };
