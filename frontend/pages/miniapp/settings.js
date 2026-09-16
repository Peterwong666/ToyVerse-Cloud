/* ============================================================
   终端用户端 · 设置
   ------------------------------------------------------------
   真实接口：

     GET  /miniapp/devices/{id}            设备详情（SN / 固件 / 网络 / 在线）
     GET  /miniapp/devices/{id}/settings   设备侧设置（音量 / 儿童模式 / 唤醒词）
     PUT  /miniapp/devices/{id}/settings   局部更新（只传改动的字段）
     POST /miniapp/devices/{id}/unbind     解绑（reason 可选）

   音量与儿童模式为什么是「写一个改一个」
   --------------------------------------
   后端 PUT 是局部更新（volume? / childMode? / wakeWord? 都可选），
   所以每改一项就只提交该项：滑一次音量不会顺手把儿童模式一起写回，
   也就不会出现「A 设备改了音量、把 B 页签刚改的儿童模式覆盖回去」这类
   并发覆盖问题。

   解绑为什么必须真的解绑
   ----------------------
   P2 的演示壳里，「解绑」只是 endUser.reset() + 清 state——设备在平台上
   仍然绑着这个账号，界面却说「已解绑」。这是**假的交付**。
   本页改为：先调 /unbind，成功后才清本地状态；失败则保留状态并说明原因。
   另外解绑只解除设备绑定，不等于退出登录——两者分开。
   ============================================================ */

import endUser from '/shared/core/end-user.js';
import { fromHtml, formatDate, h, maskPhone } from '/shared/ui/dom.js';
import { alert, emptyState } from '/shared/ui/components.js';
import { icon } from '/shared/ui/icons.js';
import { modal } from '/shared/ui/modal.js';
import toast from '/shared/ui/toast.js';
import {
  deviceName,
  mpApi,
  navigate,
  networkLabel,
  notifyError,
  registerScreen,
  setCurrentDevice,
  state,
} from './shell.js';

/** 把设备信息渲染成 mp-list 行（沿用 P2 的视觉） */
function infoRow(iconName, title, value) {
  const iconTpl = document.createElement('template');
  iconTpl.innerHTML = icon(iconName, { size: 16 }).trim();

  return h(
    'div',
    { class: 'mp-list-item' },
    h('span', { class: 'mp-list-icon' }, iconTpl.content),
    h('span', { class: 'mp-list-title', text: title }),
    h('span', { class: 'mp-list-value', text: value ?? '-' }),
    h('span', { class: 'mp-list-arrow', text: '' }),
  );
}

registerScreen('settings', {
  tab: 'settings',
  showTab: true,
  title: '设置',
  render: (body) => {
    const device = state.device;
    const section = h('div', { class: 'mp-section' });

    /* ---- 未绑定设备 ---- */
    if (!device?.id) {
      const list = h('div', { class: 'mp-list' });
      list.append(infoRow('device', '设备信息', '未绑定'));
      list.append(infoRow('user', '账号', endUser.profile?.phone || '未登录'));
      section.append(list);

      section.append(
        fromHtml(
          `<div class="mt-4">${emptyState({
            icon: 'qrcode',
            title: '还没有绑定设备',
            desc: '扫码激活设备后，这里可以调整音量、儿童模式与唤醒词。',
          })}</div>`,
        ),
      );

      const toScan = h('button', { class: 'mp-btn mt-4', type: 'button', text: '扫码添加设备' });
      toScan.addEventListener('click', () => navigate('scan'));
      section.append(toScan);

      const toLogin = h('button', {
        class: 'mp-btn mp-btn-outline mt-3',
        type: 'button',
        text: '返回管理后台登录',
      });
      toLogin.addEventListener('click', () => {
        window.location.href = '/login';
      });
      section.append(toLogin);

      body.append(section);
      return;
    }

    /* ---- 设备信息 ---- */
    const infoList = h('div', { class: 'mp-list' });
    const infoHost = h('div', { class: 'mp-section-title', text: '设备信息' });
    section.append(infoHost, infoList);

    function paintDevice(detail) {
      const merged = { ...device, ...(detail || {}) };
      infoList.replaceChildren(
        infoRow('device', '设备名称', deviceName(merged)),
        infoRow('qrcode', '设备 SN', merged.sn),
        infoRow('refresh', '固件版本', merged.firmwareVersion || '未知'),
        infoRow('wifi', '联网方式', networkLabel(merged.networkType)),
        infoRow('signal', '在线状态', merged.online ? '在线' : '离线'),
        infoRow('clock', '最近激活', merged.activatedAt ? formatDate(merged.activatedAt) : '-'),
        infoRow('building', '归属商户', merged.tenantName || '-'),
      );
    }
    paintDevice(null);

    /** 拉详情（失败不阻塞：设置项仍然可用） */
    async function loadDetail() {
      try {
        const detail = await mpApi.get(`/miniapp/devices/${device.id}`);
        paintDevice(detail);
        // 详情里带 networkType/online 等最新值，回填到当前设备，首页也跟着更新
        setCurrentDevice({ ...device, ...detail });
      } catch (error) {
        if (error?.code === 'DEVICE_NOT_FOUND' || error?.code === 'DEVICE_NOT_IN_TENANT') {
          toast.warning('设备已不在你的账号下，本地缓存已清除');
          setCurrentDevice(null);
          navigate('scan');
          return;
        }
        /* 其余错误静默：SN/固件只是展示信息，不该拦住音量与儿童模式 */
      }
    }

    /* ---- 我的账号（GET /miniapp/profile） ---- */
    const accountTitle = h('div', { class: 'mp-section-title mt-4', text: '我的账号' });
    const accountList = h('div', { class: 'mp-list' });
    section.append(accountTitle, accountList);

    function paintAccount(profile) {
      accountList.replaceChildren(
        infoRow('user', '昵称', profile?.nickname || '-'),
        infoRow('lock', '手机号', maskPhone(profile?.phone)),
        infoRow('device', '账号下设备数', profile?.deviceCount ?? state.devices?.length ?? 0),
      );
    }
    // 先用登录时缓存的资料渲染，再用 /profile 覆盖（资料可能已在别处改过）
    paintAccount(endUser.profile);

    /* ---- 设备设置（音量 / 儿童模式 / 唤醒词） ---- */
    const settingsTitle = h('div', { class: 'mp-section-title mt-4', text: '设备设置' });
    const settingsCard = h('div', { class: 'mp-card' });
    const settingsHint = h('div', { class: 'field-hint' });
    section.append(settingsTitle, settingsCard, settingsHint);

    const volumeLabel = h('span', { class: 'text-sm text-secondary', text: '-' });
    const volumeInput = h('input', {
      type: 'range',
      min: '0',
      max: '100',
      step: '1',
      value: '50',
      style: { width: '100%' },
      'aria-label': '音量',
    });
    volumeInput.disabled = true;

    const childToggle = h('input', { type: 'checkbox', 'aria-label': '儿童模式' });
    childToggle.disabled = true;
    const childLabel = h('span', { class: 'switch-label', text: '儿童模式' });
    const childSwitch = h('label', { class: 'switch' }, childToggle, h('span', { class: 'switch-track' }), childLabel);

    const wakeInput = h('input', { class: 'input', type: 'text', placeholder: '例如：你好小玩具', 'aria-label': '唤醒词' });
    wakeInput.disabled = true;
    const wakeBtn = h('button', { class: 'btn', type: 'button', text: '保存', style: { flexShrink: '0' } });
    wakeBtn.disabled = true;

    settingsCard.append(
      h(
        'div',
        { class: 'field' },
        h('label', { class: 'field-label' }, h('span', { text: '音量' }), volumeLabel),
        volumeInput,
      ),
      h('div', { class: 'mt-4' }, childSwitch),
      h(
        'div',
        { class: 'field mt-4' },
        h('label', { class: 'field-label', text: '唤醒词' }),
        h('div', { class: 'flex gap-2' }, wakeInput, wakeBtn),
        h('div', { class: 'field-hint', text: '修改后设备下次唤醒即生效（设备在线时生效更快）。' }),
      ),
    );

    /** PUT 局部更新；成功后就地更新 state，失败回滚到服务端值 */
    async function saveSettings(patch, rollback) {
      settingsHint.classList.remove('text-danger');
      settingsHint.textContent = '正在保存…';
      try {
        const result = await mpApi.put(`/miniapp/devices/${device.id}/settings`, patch);
        paintSettings(result);
        settingsHint.textContent = '已保存';
        return true;
      } catch (error) {
        rollback?.();
        settingsHint.classList.add('text-danger');
        settingsHint.textContent = '保存失败，已还原为设备当前值。';
        notifyError(error, '设置保存失败');
        return false;
      }
    }

    let current = null;

    function paintSettings(settings) {
      current = settings || {};
      const hasVolume = Number.isFinite(Number(current.volume));
      volumeInput.disabled = !hasVolume;
      volumeInput.value = hasVolume ? String(Number(current.volume)) : '0';
      volumeLabel.textContent = hasVolume ? `${Number(current.volume)} / 100` : '暂不支持';

      childToggle.disabled = false;
      childToggle.checked = Boolean(current.childMode);
      childLabel.textContent = current.childMode ? '儿童模式（已开启）' : '儿童模式（未开启）';

      wakeInput.disabled = false;
      wakeInput.value = current.wakeWord || '';
      wakeBtn.disabled = false;

      settingsHint.textContent = current.updatedAt ? `最近更新：${formatDate(current.updatedAt)}` : '';
    }

    async function loadSettings() {
      settingsHint.textContent = '正在读取设备设置…';
      try {
        const settings = await mpApi.get(`/miniapp/devices/${device.id}/settings`);
        paintSettings(settings);
      } catch (error) {
        volumeInput.disabled = true;
        wakeInput.disabled = true;
        wakeBtn.disabled = true;
        settingsHint.classList.add('text-danger');
        settingsHint.textContent = '设备设置读取失败，暂时无法修改。';
        if (error?.code !== 'DEVICE_NOT_FOUND') notifyError(error, '设置读取失败');
      }
    }

    /* 拖动时只更新文案，松手（change）才提交：避免一次拖动发出几十个请求 */
    volumeInput.addEventListener('input', () => {
      volumeLabel.textContent = `${Number(volumeInput.value)} / 100`;
    });
    volumeInput.addEventListener('change', () => {
      const next = Number(volumeInput.value);
      const previous = current?.volume;
      saveSettings({ volume: next }, () => {
        volumeInput.value = String(Number(previous) || 0);
        volumeLabel.textContent = `${Number(previous) || 0} / 100`;
      });
    });

    childToggle.addEventListener('change', () => {
      const next = childToggle.checked;
      const previous = Boolean(current?.childMode);
      saveSettings({ childMode: next }, () => {
        childToggle.checked = previous;
        childLabel.textContent = previous ? '儿童模式（已开启）' : '儿童模式（未开启）';
      });
    });

    wakeBtn.addEventListener('click', () => {
      const next = wakeInput.value.trim();
      if (!next) {
        toast.warning('唤醒词不能为空');
        wakeInput.focus();
        return;
      }
      wakeBtn.disabled = true;
      saveSettings({ wakeWord: next }).finally(() => {
        wakeBtn.disabled = false;
      });
    });

    /* ---- 解绑 ---- */
    const unbindBtn = h('button', {
      class: 'mp-btn mp-btn-outline',
      type: 'button',
      text: '解绑设备',
      style: { marginTop: 'var(--space-4)', color: 'var(--danger-600)' },
    });

    unbindBtn.addEventListener('click', async () => {
      const dialog = modal({
        title: '解绑设备',
        size: 'sm',
        body: () => {
          const wrap = h('div');
          wrap.append(
            fromHtml(
              alert({
                tone: 'danger',
                title: `确认解绑 ${deviceName(device)}？`,
                text: '解绑后这台设备将不再属于你的账号，需要重新扫码才能再次绑定。平台上的绑定关系会同步解除。',
              }),
            ),
          );
          const reason = h('textarea', {
            class: 'textarea mt-3',
            rows: '3',
            placeholder: '解绑原因（可选）',
          });
          wrap.append(
            h(
              'div',
              { class: 'field mt-3' },
              h('label', { class: 'field-label', text: '解绑原因（可选）' }),
              reason,
              h('div', { class: 'field-hint', text: '填写后会写入绑定记录与审计日志，便于售后追溯。' }),
            ),
          );
          return wrap;
        },
        footer: (dialogApi) => {
          const cancel = h('button', { class: 'btn', type: 'button', text: '取消' });
          const ok = h('button', { class: 'btn btn-danger', type: 'button', text: '确认解绑' });

          cancel.addEventListener('click', () => dialogApi.close(null));
          ok.addEventListener('click', async () => {
            const reason = dialogApi.body.querySelector('textarea')?.value.trim() || '';
            ok.disabled = true;
            ok.textContent = '解绑中…';
            try {
              /* 先调接口再改本地状态：接口失败就不能说「已解绑」 */
              await mpApi.post(`/miniapp/devices/${device.id}/unbind`, reason ? { reason } : {});
              dialogApi.close({ unbound: true });
            } catch (error) {
              ok.disabled = false;
              ok.textContent = '确认解绑';
              notifyError(error, '解绑失败');
            }
          });

          return [cancel, ok];
        },
      });

      const outcome = await dialog.result;
      if (!outcome?.unbound) return;

      toast.success('设备已解绑');
      // 只清这台设备与「当前设备」，登录态保留（解绑 ≠ 退出登录）
      state.devices = (state.devices || []).filter((item) => item?.id !== device.id);
      setCurrentDevice(null);
      endUser.setDeviceId(null);
      navigate('scan');
    });

    section.append(unbindBtn);

    /* ---- 退出登录：清终端用户会话，不影响后台管理端 ---- */
    const logoutBtn = h('button', {
      class: 'mp-btn mp-btn-outline',
      type: 'button',
      text: '退出登录',
      style: { marginTop: 'var(--space-3)' },
    });
    logoutBtn.addEventListener('click', () => {
      endUser.clear();
      state.devices = [];
      setCurrentDevice(null);
      toast.info('已退出登录（设备绑定关系仍保留在平台上）');
      navigate('login');
    });
    section.append(logoutBtn);

    /* ---- 返回管理后台 ---- */
    const toLogin = h('button', {
      class: 'mp-btn mp-btn-outline',
      type: 'button',
      text: '返回管理后台登录',
      style: { marginTop: 'var(--space-3)' },
    });
    toLogin.addEventListener('click', () => {
      window.location.href = '/login';
    });
    section.append(toLogin);

    body.append(section);

    loadDetail();
    loadSettings();

    /* 资料只用于展示，拉取失败不影响设置功能 */
    mpApi
      .get('/miniapp/profile')
      .then((profile) => paintAccount(profile))
      .catch(() => {});
  },
});
