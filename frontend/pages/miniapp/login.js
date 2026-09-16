/* ============================================================
   终端用户端 · 手机号登录
   ------------------------------------------------------------
   真实接口（对应后端 /miniapp/auth/*）：

     POST /miniapp/auth/code   { phone }
          → { sent, mock, mockCode, expiresInSeconds, message }
     POST /miniapp/auth/login  { phone, code }
          → { accessToken, tokenType, expiresInSeconds, user, devices }

   为什么演示环境要把验证码回显并自动填入
   --------------------------------------
   演示环境没有短信通道（或用的是 mock 通道），若不给 mockCode，
   演示时第一步就卡死。后端已经把「是否回显」这件事变成响应里的
   mock 标记，前端要做的只有**如实标注**：界面上必须写明
   「演示环境：验证码已自动填入」，不能让人误以为真实下发了短信。

   登录态写入 shared/core/end-user.js（终端用户会话），
   与后台管理端的 auth.js **完全隔离**——后台登录态既不能让这里
   跳过登录，这里也不会污染后台令牌。
   ============================================================ */

import endUser from '/shared/core/end-user.js';
import { fromHtml, h } from '/shared/ui/dom.js';
import { alert } from '/shared/ui/components.js';
import toast from '/shared/ui/toast.js';
import {
  mpApi,
  navigate,
  notifyError,
  registerScreen,
  setCurrentDevice,
  state,
} from './shell.js';

/** 重新获取验证码的冷却秒数（与后端默认有效期同一量级即可） */
const RESEND_COOLDOWN_SECONDS = 60;

/** 冷却定时器：放模块级，离开本屏时才能清掉（否则切走后仍在空转） */
let cooldownTimer = null;

function stopCooldown() {
  clearInterval(cooldownTimer);
  cooldownTimer = null;
}

registerScreen('login', {
  title: '手机号登录',
  backTo: 'scan',
  render: (body) => {
    /* 已有有效令牌：不在登录页停留。
       有设备 → 首页（继续用玩具）；没有设备 → 去扫码激活。 */
    if (endUser.isLoggedIn) {
      navigate(state.devices?.length || endUser.deviceId ? 'home' : 'scan');
      return;
    }

    const section = h('div', { class: 'mp-section' });
    const hintHost = h('div', { class: 'mb-3' });

    /* ---- 表单 ---- */
    const phoneInput = h('input', {
      class: 'input',
      type: 'tel',
      placeholder: '请输入手机号',
      inputmode: 'numeric',
      maxlength: '11',
      autocomplete: 'tel',
      'aria-label': '手机号',
    });

    const codeInput = h('input', {
      class: 'input',
      type: 'text',
      placeholder: '验证码',
      inputmode: 'numeric',
      maxlength: '6',
      autocomplete: 'one-time-code',
      'aria-label': '验证码',
    });

    const codeBtn = h('button', {
      class: 'btn',
      type: 'button',
      text: '获取验证码',
      style: { flexShrink: '0' },
    });

    const submitBtn = h('button', { class: 'mp-btn', type: 'button', text: '登录并继续' });

    const form = h(
      'div',
      { style: { display: 'flex', flexDirection: 'column', gap: 'var(--space-3)' } },
      phoneInput,
      h('div', { class: 'flex gap-2' }, codeInput, codeBtn),
      submitBtn,
    );

    /* ---- 冷却倒计时 ---- */

    function startCooldown(seconds) {
      let remaining = Math.max(1, Number(seconds) || RESEND_COOLDOWN_SECONDS);
      stopCooldown();
      codeBtn.disabled = true;

      const paint = () => {
        codeBtn.textContent = `重新获取（${remaining}s）`;
      };
      paint();

      cooldownTimer = setInterval(() => {
        remaining -= 1;
        if (remaining <= 0) {
          stopCooldown();
          codeBtn.disabled = false;
          codeBtn.textContent = '获取验证码';
          return;
        }
        paint();
      }, 1000);
    }

    /** 读取手机号并做基本校验（与后端 PHONE_PATTERN 同为 ^1[3-9]\d{9}$） */
    function readPhone() {
      const phone = phoneInput.value.trim();
      if (!/^1[3-9]\d{9}$/.test(phone)) {
        toast.warning('请输入正确的 11 位手机号');
        phoneInput.focus();
        return null;
      }
      return phone;
    }

    /* ---- 获取验证码 ---- */
    codeBtn.addEventListener('click', async () => {
      const phone = readPhone();
      if (!phone) return;

      codeBtn.disabled = true;
      codeBtn.textContent = '发送中…';
      try {
        const result = await mpApi.post('/miniapp/auth/code', { phone });
        startCooldown(result?.expiresInSeconds || RESEND_COOLDOWN_SECONDS);

        hintHost.replaceChildren();
        if (result?.mock && result?.mockCode) {
          // 演示环境：验证码回显 → 自动填入 + 明确标注，省掉一次手工抄写
          codeInput.value = result.mockCode;
          hintHost.append(
            fromHtml(
              alert({
                tone: 'info',
                title: '演示环境：验证码已自动填入',
                text: `短信通道为模拟（未真实下发）。验证码 ${result.mockCode}，${
                  result.expiresInSeconds || 300
                } 秒内有效。`,
              }),
            ),
          );
        } else {
          hintHost.append(
            fromHtml(
              alert({
                tone: 'success',
                text: result?.message || '验证码已发送，请查看短信。',
              }),
            ),
          );
        }
        toast.success('验证码已发送');
      } catch (error) {
        codeBtn.disabled = false;
        codeBtn.textContent = '获取验证码';
        // 503 VENDOR_UNAVAILABLE = 短信通道未配置：安全失败，不伪造成功
        if (error?.code === 'VENDOR_UNAVAILABLE') {
          hintHost.replaceChildren(
            fromHtml(
              alert({
                tone: 'warning',
                title: '短信通道未配置',
                text: '平台未配置短信供应商，验证码无法下发。该能力已安全禁用——不会伪造一条“已发送”。请联系平台管理员配置后重试。',
              }),
            ),
          );
          return;
        }
        notifyError(error, '验证码发送失败');
      }
    });

    /* ---- 提交登录 ---- */
    submitBtn.addEventListener('click', async () => {
      const phone = readPhone();
      if (!phone) return;

      const code = codeInput.value.trim();
      if (!code) {
        toast.warning('请输入验证码');
        codeInput.focus();
        return;
      }

      submitBtn.disabled = true;
      submitBtn.textContent = '登录中…';
      try {
        const result = await mpApi.post('/miniapp/auth/login', { phone, code });

        // 令牌存进终端用户会话（与后台管理端令牌互不覆盖）
        endUser.save({
          token: result?.accessToken,
          expiresIn: result?.expiresInSeconds,
          profile: result?.user || { phone },
        });

        state.devices = Array.isArray(result?.devices) ? result.devices : [];

        // 优先沿用上次使用的设备（可能已失效 → 回退到第一台）
        const storedId = endUser.deviceId;
        const chosen = state.devices.find((item) => item?.id === storedId) || state.devices[0] || null;
        if (chosen) setCurrentDevice(chosen);

        toast.success('登录成功');
        navigate(chosen ? 'home' : 'scan');
      } catch (error) {
        submitBtn.disabled = false;
        submitBtn.textContent = '登录并继续';

        if (error?.code === 'VALIDATION_ERROR') {
          /* 400 有两个来源：验证码不匹配（带 details.attemptsLeft）与
             字段格式不合法（手机号 / 验证码长度，不带该字段）。
             用 attemptsLeft 是否存在来区分，否则格式错误会被说成「验证码不正确」。 */
          const left = error?.details?.attemptsLeft;

          if (typeof left !== 'number') {
            hintHost.replaceChildren(
              fromHtml(
                alert({
                  tone: 'danger',
                  title: '登录信息有误',
                  text: error?.message || '请检查手机号与验证码格式后重试。',
                }),
              ),
            );
            return;
          }

          codeInput.value = '';
          codeInput.focus();
          hintHost.replaceChildren(
            fromHtml(
              alert({
                tone: 'danger',
                title: '验证码不正确',
                text: `请重新输入。剩余尝试次数：${left} 次（次数用尽后需重新获取验证码）。`,
              }),
            ),
          );
          return;
        }

        if (error?.code === 'QR_EXPIRED') {
          codeInput.value = '';
          hintHost.replaceChildren(
            fromHtml(
              alert({
                tone: 'warning',
                title: '验证码已过期',
                text: '请点击「获取验证码」重新获取后再登录。',
              }),
            ),
          );
          return;
        }

        if (error?.code === 'VENDOR_UNAVAILABLE') {
          hintHost.replaceChildren(
            fromHtml(
              alert({
                tone: 'warning',
                title: '短信通道未配置',
                text: '验证码校验能力被安全禁用，无法登录。请联系平台管理员配置短信供应商。',
              }),
            ),
          );
          return;
        }

        notifyError(error, '登录失败');
      }
    });

    section.append(
      hintHost,
      h(
        'div',
        { class: 'mp-card' },
        h('div', { class: 'mp-section-title', text: '登录后即可绑定设备' }),
        h('div', {
          class: 'text-xs text-secondary mb-4',
          text: '登录用于关联设备与你的账号，便于后续管理',
        }),
        form,
      ),
      fromHtml(
        `<div class="mt-4">${alert({
          tone: 'neutral',
          text: '终端用户会话与后台管理端完全隔离：在这里登录不会影响（也不会使用）管理后台的登录态。',
        })}</div>`,
      ),
    );

    /* 离开登录页时清掉倒计时，避免定时器在后台空转 */
    body.append(section);
  },
  onLeave: stopCooldown,
});
