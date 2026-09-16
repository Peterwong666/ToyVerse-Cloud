/* ============================================================
   终端用户端 · Wi-Fi 配网激活（京东 JoyInside 方案）
   ------------------------------------------------------------
   真实流程（对应 POST /miniapp/devices/{id}/activate-wifi）：

     设备连上家庭 Wi-Fi 后向平台上报自己读到的 SN 与 MAC →
     平台校验「设备上报的 SN」与「二维码里的 SN」是否一致 →
       一致   → 携带厂商密钥调京东接口激活
       不一致 → 409 BIND_FAILED（details.qrSn / details.reportedSn）

   为什么页码要把「SN 一致性」做成显式分支
   ---------------------------------------
   这是全流程里**唯一**一处「两种 SN 可能不一样」的地方，也是最容易
   被糊弄过去的地方：如果前端把 409 当成普通错误弹一句「激活失败」，
   家长既不知道哪里错了，也无法自助解决。因此这里：

     * 提交前就并排显示「二维码 SN」和输入框里的「设备上报 SN」；
     * 409 时单独渲染两个 SN 的对照，并说明下一步动作（对照机身标签）。

   历史背景：京东方案的 SN 一致性校验是防「贴错码/换壳」的关键闸门，
   校验不过就不调厂商接口——所以它同样是安全失败，平台不会先激活再补校验。
   ============================================================ */

import { fromHtml, h } from '/shared/ui/dom.js';
import { alert, descList, emptyState } from '/shared/ui/components.js';
import toast from '/shared/ui/toast.js';
import { mpApi, navigate, notifyError, registerScreen, setCurrentDevice, state } from './shell.js';

/** 种子数据里的 Wi-Fi 演示设备（backend/app/db/seed.py），供演示一键填入 */
const DEMO_WIFI = { sn: 'SN-DEMO-WIFI-001', mac: 'AA:BB:CC:00:01:01' };

registerScreen('setup_wifi', {
  title: '连接 Wi-Fi',
  backTo: 'scan',
  render: (body) => {
    const pending = state.pendingDevice;
    const section = h('div', { class: 'mp-section' });

    if (!pending?.id) {
      section.append(
        fromHtml(
          emptyState({
            icon: 'qrcode',
            title: '还没有待配网的设备',
            desc: '请先扫描玩具底部的二维码，识别出设备后再配网激活。',
          }),
        ),
      );
      const toScan = h('button', { class: 'mp-btn mt-4', type: 'button', text: '去扫码' });
      toScan.addEventListener('click', () => navigate('scan'));
      section.append(toScan);
      body.append(section);
      return;
    }

    const qrSn = pending.sn || '';

    /* ---- 顶部说明：把校验规则先讲清楚，失败时才不突兀 ---- */
    const ruleAlert = fromHtml(
      alert({
        tone: 'info',
        title: '先校验 SN 一致，再调厂商激活',
        text:
          '设备连上 Wi-Fi 后会向平台上报它自己读到的 SN 与 MAC。' +
          `平台会先把上报的 SN 与二维码里的 SN（${qrSn || '-'}）逐字比对，` +
          '再与设备档案里的 MAC 比对（忽略大小写），两项都通过才调用京东接口激活；' +
          '任一项不一致都会直接拒绝，不会激活。',
      }),
    );

    /* ---- 表单 ---- */
    const snInput = h('input', {
      class: 'input',
      type: 'text',
      placeholder: '设备上报的 SN',
      value: qrSn,
      autocapitalize: 'characters',
      spellcheck: 'false',
      'aria-label': '设备 SN',
    });

    const macInput = h('input', {
      class: 'input',
      type: 'text',
      placeholder: 'MAC，形如 AA:BB:CC:00:01:01',
      autocapitalize: 'characters',
      spellcheck: 'false',
      'aria-label': '设备 MAC',
    });

    const snField = h(
      'div',
      { class: 'field' },
      h('label', { class: 'field-label', text: '设备上报的 SN' }),
      snInput,
      h('div', {
        class: 'field-hint',
        text: `默认填入二维码 SN；若设备实际上报的 SN 不同，请改成设备上报的那个（机身标签）。`,
      }),
    );

    const macField = h(
      'div',
      { class: 'field mt-3' },
      h('label', { class: 'field-label', text: '设备 MAC' }),
      macInput,
      h('div', { class: 'field-hint', text: '配网成功后由设备上报，可从设备端日志或配网工具查看。' }),
    );

    const submitBtn = h('button', { class: 'mp-btn mt-4', type: 'button', text: '提交配网结果并激活' });

    const demoBtn = h('button', {
      class: 'mp-btn mp-btn-outline mt-2',
      type: 'button',
      text: '填入演示设备（种子数据）',
    });

    const resultHost = h('div', { class: 'mb-3' });

    demoBtn.addEventListener('click', () => {
      snInput.value = DEMO_WIFI.sn;
      macInput.value = DEMO_WIFI.mac;
      toast.info('已填入种子数据：SN-DEMO-WIFI-001 / AA:BB:CC:00:01:01');
    });

    submitBtn.addEventListener('click', async () => {
      const sn = snInput.value.trim();
      const mac = macInput.value.trim();

      if (!sn) {
        toast.warning('请填写设备上报的 SN');
        snInput.focus();
        return;
      }
      if (!mac) {
        toast.warning('请填写设备 MAC');
        macInput.focus();
        return;
      }
      /* MAC 只做形状校验（允许 AA:BB:CC:00:01:01 / AA-BB-… / 12 位纯十六进制）：
         后端的口径是「1–32 字符、大小写不敏感」，前端不能比它更严——
         设备端上报格式五花八门，多拦一种形态就等于多一类打不通的客服单。 */
      if (!/^([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}$|^[0-9a-fA-F]{12}$/.test(mac)) {
        toast.warning('MAC 格式应为 AA:BB:CC:00:01:01（或 12 位十六进制）');
        macInput.focus();
        return;
      }

      submitBtn.disabled = true;
      submitBtn.textContent = '校验并激活中…';
      resultHost.replaceChildren();

      try {
        const result = await mpApi.post(`/miniapp/devices/${pending.id}/activate-wifi`, { sn, mac });

        state.lastActivation = result;
        setCurrentDevice({
          id: result?.deviceId || pending.id,
          sn: result?.sn || sn,
          networkType: 'WIFI',
          activationStatus: result?.activationStatus,
          bindStatus: result?.bindStatus,
          productName: pending.productName,
          tenantName: pending.tenantName,
          online: false,
        });

        if (result?.activationStatus === 'ACTIVATED' || result?.bindStatus === 'BOUND') {
          toast.success('配网校验通过，激活成功');
          navigate('activate_done');
          return;
        }

        resultHost.append(
          fromHtml(
            alert({
              tone: 'warning',
              title: '激活未完成',
              text: `厂商返回：${result?.vendorMessage || '状态未知'}（当前激活状态 ${
                result?.activationStatus || '-'
              }）。`,
            }),
          ),
        );
      } catch (error) {
        /* ---- 显式分支：校验不一致（本页最需要讲清楚的一种失败） ----
           BIND_FAILED 有两个来源，靠 details 区分：
             SN 不一致 → { qrSn, reportedSn }
             MAC 不一致 → { expectedMac, reportedMac }
           两者都不能笼统说成「激活失败」——用户要改的字段不一样。 */
        if (error?.code === 'BIND_FAILED') {
          const details = error?.details || {};
          const macMismatch = details.expectedMac !== undefined || details.reportedMac !== undefined;

          if (macMismatch) {
            resultHost.replaceChildren(
              fromHtml(
                alert({
                  tone: 'danger',
                  title: '设备上报的 MAC 与平台记录不一致',
                  text:
                    '平台已拒绝调用厂商激活接口。请核对设备 MAC（在设备端日志或配网工具中查看）；' +
                    '若确认无误仍不一致，请联系卖家核实设备档案。',
                }),
              ),
              fromHtml(
                descList(
                  [
                    ['平台记录的 MAC', details.expectedMac],
                    ['你填写的 MAC', details.reportedMac ?? mac],
                  ],
                  { cols: 1 },
                ),
              ),
            );
            return;
          }

          const fromQr = details.qrSn ?? qrSn;
          const reported = details.reportedSn;

          resultHost.replaceChildren(
            fromHtml(
              alert({
                tone: 'danger',
                title: '二维码上的 SN 与你填写的 SN 不一致',
                text:
                  '平台已拒绝调用厂商激活接口（不会激活一台对不上号的设备）。' +
                  '请对照玩具机身标签核对 SN；若确认标签 SN 与二维码不同，说明二维码与设备不匹配，请联系卖家。',
              }),
            ),
            fromHtml(
              descList(
                [
                  ['二维码上的 SN', fromQr],
                  ['你填写的 SN（设备上报）', reported ?? sn],
                ],
                { cols: 1 },
              ),
            ),
          );

          // 把输入框回填成二维码 SN，避免用户在原值上反复试
          if (fromQr) snInput.value = fromQr;
          return;
        }

        if (error?.code === 'VENDOR_UNAVAILABLE') {
          resultHost.replaceChildren(
            fromHtml(
              alert({
                tone: 'warning',
                title: '厂商密钥未配置，配网激活能力已安全禁用（不会伪造成功）',
                text:
                  '平台未配置京东 JoyInside 云服务的密钥，无法调用厂商接口。' +
                  '设备状态保持原样，配置密钥后重试即可。请联系平台管理员。',
              }),
            ),
          );
          return;
        }

        if (error?.code === 'INVALID_STATE_TRANSITION' || error?.code === 'DEVICE_ALREADY_BOUND') {
          resultHost.replaceChildren(
            fromHtml(
              alert({
                tone: 'info',
                title: '设备当前状态无需再次激活',
                text: error?.message || '该设备可能已被激活或已绑定账号。',
              }),
            ),
          );
          return;
        }

        notifyError(error, '配网激活失败');
      } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = '提交配网结果并激活';
      }
    });

    section.append(
      resultHost,
      ruleAlert,
      h(
        'div',
        { class: 'mp-card mt-4' },
        h('div', { class: 'mp-section-title', text: '为玩具配置网络' }),
        h('div', {
          class: 'text-xs text-secondary mb-4',
          text: '在手机上完成配网（玩具连上家庭 Wi-Fi）后，把设备上报的 SN 与 MAC 提交给平台',
        }),
        snField,
        macField,
        submitBtn,
        demoBtn,
      ),
      fromHtml(
        `<div class="mt-4">${alert({
          tone: 'neutral',
          text: '配网本身由设备端完成：玩具联网后才会上报 SN/MAC。本页只负责把它提交给平台做一致性校验与激活。',
        })}</div>`,
      ),
    );

    body.append(section);
  },
});
