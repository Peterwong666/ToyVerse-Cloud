# 变更日志

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

---

## [未发布]

> 暂无。下一次变更请在此登记。

---

## [1.0.0] - 2026-09-17

> **首个功能完整版本**：P0–P11 十二个阶段全部交付（46 张表 / 145 个端点 / 54 个权限码 /
> 14 篇专业文档 + 文档一致性门禁 / 学习手册 20 文件 8177 行 / 14 张各端截图）。
> ⚠️ **本版本已知的边界**：AI 与设备链路未与真实厂商联调、未实现儿童数据合规（PIPL/COPPA）、
> 代码覆盖率 70%、未做压测 —— 详见下方「计划中」与各文档的「已知局限」小节。

### 新增

**P1 后端地基**

- 配置与启动期安全校验：弱口令 / 默认密钥一律拒绝启动（`InsecureConfigurationError`）
- 统一错误码（25 个）与统一响应契约 `{code, message, traceId}`；`traceId` 中间件贯穿日志与响应头
- 权限目录（6 类角色）与四层 Alembic 迁移，SQLAlchemy 2.0 模型共 11 张表
- 鉴权：bcrypt 口令 + JWT 访问/刷新令牌，含登录失败锁定（5 次 / 15 分钟）与刷新令牌重放检测
- 租户作用域在 repository 层收口（ADR-08）、幂等键、分页、审计服务，8 个 API 端点
- 幂等种子数据；修复遗留缺陷 **P-04**（禁用租户登录被误判为账号问题）

**P2 前端地基**

- 全新视觉设计系统（零构建 ES Module）：靛紫 `#4F46E5` 主色、浅色侧导轨、Inter + Noto Sans SC
- `shared/core`：契约先行的 API 客户端（`Authorization` / `x-trace-id` / 统一错误解析 / 401 自动刷新）、token 与权限、**参数化 hash 路由**、路由守卫、pub/sub store、WebSocket 客户端（含 SSE 降级）
- `shared/ui`：组件库、表格、表单、Modal/Drawer、通知、纯 SVG 图表、**Canvas 手写二维码编码器**、⌘K 命令面板、DOM 安全转义
- 四端入口与统一登录页（对接真实 `/auth/login`，移除原型「任意密码可登录」）
- 修复遗留缺陷 **P-01**（刷新子页面状态丢失）、**P-02**（`back()` 目标错误）、**P-05**（无路由守卫）
- 新增质量门禁 `make fe-check`（77 处 ES Module 导入契约）与 `make qr-verify`（104 组二维码矩阵与独立实现比对）

**P3 目录域**

- **数据层**：Alembic `0005_catalog` / `0006_client_product_miniapp`，新增 5 张表（`cloud_providers` / `product_templates` / `product_authorizations` / `client_products` / `miniapp_configs`），与 ORM 零漂移、升降可逆
- **密钥保护**：新增 `app/core/crypto.py`（Fernet 对称加密）；云服务商 `accessKey` / `secretKey` 与小程序 `appSecret` **只存密文 + 掩码提示**，任何响应不含明文——附录 B 中「SecretKey 明文下发前端」问题的架构性修复
- **服务层**：`tenant_service`（客户开通 / 启停 / 账号与一次性密码）、`catalog_service`（云服务商 CRUD + 连通性检测、模板 CRUD + 授权、客户产品 CRUD + 小程序配置）
- **API**：平台端 17 个端点（`/platform/tenants`、`/clouds`、`/templates`、`/client-products` 及授权、小程序配置等子资源）；OpenAPI 契约快照已更新（31 个路径）
- **安全语义**：连通性检测在未配置密钥时返回 `NOT_CONFIGURED` 且**跳过真实调用**（ADR-07，浏览器实测验证）；目录域「编码重复」统一为 409（新增 `CLOUD_CODE_EXISTS` / `TEMPLATE_CODE_EXISTS`，复用既有 `PRODUCT_CODE_EXISTS`）
- **前端**：平台端新增 6 个页面（租户列表 / 租户详情 / 产品模板 / 云服务商 / 客户产品 / 客户产品详情）与页面共用工具 `pages/platform/common.js`；**首次启用参数化路由** `#/tenants/:id`、`#/client-products/:id`
- **演示数据**：种子数据补充 2 个云服务商（含**火山引擎智能云 · 硬件对话智能体**）、2 个产品模板、3 条授权、2 个客户产品

**P4 订单与设备生成**

- **数据层**：Alembic `0008_orders` / `0009_devices_batches` / `0010_factory_production`，新增 9 张表（`orders` / `device_batches` / `devices` / `device_batch_lines` / `device_credentials` / `device_events` / `factory_orders` / `burn_reports` / `inspections`），与 ORM 零漂移、升降可逆
- **二维码双格式**：`qrcode_service` 生成与解析 —— 集贤 4G `JX|{SN}|{IMEI}|{ICCID}|{deviceId}`、京东 Wi-Fi `JD|{tenantId}|{productId}|{sn}|{sign}`（HMAC-SHA256 + `compare_digest` 验签）；**修正原型把 `clientId` 误传入 `tenantId` 位置的缺陷**，并用单测钉死字段顺序
- **订单状态机**：`PENDING_AUDIT → APPROVED|REJECTED → GENERATING → GENERATED → IN_STOCK → PRODUCING → SHIPPED_TO_CLIENT → COMPLETED`（另有厂商失败时的 `GENERATING → APPROVED` 回退边），迁移一律经 `ORDER_TRANSITIONS` 校验
- **设备四维状态流转**：`device_service` 提供状态迁移、冻结/解冻/报废（原因必填）与**流转时间线**（`device_events` 记录维度、前后状态、操作者与 traceId）
- **批次 CSV 导入**：上传 → SHA-256 去重 → 预检 → 导入 → 错误报告 → 幂等重跑；三重安全限额（5000 行 / 5MB / 按字段长度上限）
- **API**：平台端 20 个端点（订单审核/生成/二维码、设备列表/详情/事件/冻结解冻报废、批次上传/导入/明细/错误报告）+ 商户端 4 个（下单、查单、产品列表）
- **前端**：平台端订单 / 订单详情 / 设备库存 / 批次 4 页 + 商户端「我的订单」；订单详情含**真实二维码渲染**与 CSV 导出
- **安全语义**：4G 生成设备在厂商未配置密钥时返回 `VENDOR_UNAVAILABLE`，订单退回 `APPROVED`、**设备表零新增**、审计留痕（ADR-07，浏览器与集成测试双重验证）

**P7 AI 抽象与离线模拟引擎**

- `app/ai/base.py`：`AIProvider` 抽象（生命周期 / 设备侧 / 对话侧流式 / 语音侧）与 DTO
- `app/ai/registry.py`：四层供应商解析顺序（`client_product → template.vendor → ai_providers 默认 → AI_DEFAULT_PROVIDER`）
- `app/ai/mock/*`：规则对话（故事 / 歌 / 天气 + 兜底）、ASR `echo|fixed`、TTS `text|wav`，固定随机种子可复现，**无任何密钥即可跑通全流程**
- 四个厂商适配器骨架（集贤 / JoyInside / 火山引擎 / 百度）；**火山引擎按官方「硬件对话智能体」文档校对**：统一 `POST https://rtc.volcengineapi.com?Action=<Action>&Version=2025-08-01`、`Aibot*` / `IotVoicePrint*` / `TrainTTSVoiceType` 等 Action 与 `AibotCreate` 的 `Name` / `AccessType(public|private)` / `Config.ASRConfig.*` 结构
- Alembic `0007_ai_dialogue`（8 张表）；`app/api/v1/ai.py` 六个端点（`/ai/chat` 为流式）
- **已知局限**：厂商签名算法与部分 Body 字段名仍为占位（官方《调用方法》页不可读），已在代码中显式标注，待联调替换；`ADR-07` 安全失败行为已由测试钉住

**P5 设备 / 分配 / 绑定**

- **数据层**：Alembic `0011_allocations_bindings`，新增 3 张表（`allocation_orders` / `allocation_items` / `device_bindings`），与 ORM 零漂移、升降可逆
  - 5 分钟 confirm-token **不额外建表**：以 `device_bindings.status=PENDING` + `confirm_token_hash` 落在绑定行上，`bind` 成功时把摘要置空即「销毁」；`UNIQUE(device_id)` 把**单绑约束下沉到数据库层**（并发下也不依赖服务层的「先查后写」）
  - 新增 `BindingRecordStatus`（PENDING/BOUND/UNBOUND）与设备维度的 `BindingStatus`（BOUND/UNBOUND）**分开**：合成一个枚举会造出「设备已绑定、但记录还在等待确认」这种类型上无法自洽的状态
- **分配链路** `allocation_service`：创建（校验租户 ACTIVE、客户产品归属）/ 列表 / 详情 / 明细 / 幂等执行
  - 逐行校验并**部分失败不回滚**（成功的行必须保留，否则重跑会重复分配）；`FAILED → EXECUTING` 允许重跑（只补未成功的行），`COMPLETED` 再次调用是**幂等回放**；单数与明细行逐条给出失败原因
- **绑定链路** `binding_service`：`precheck` → `bind` 两步
  - `precheck`：解析格式 → 按 SN 查设备 → **SHA-256 命中**（用设备记录重算规范载荷比对摘要，因此无签名的集贤 `JX` 格式同样无法被篡改）→ 租户匹配 → 冻结拦截 → 单绑约束 → 可绑状态（只接受 `ALLOCATED`）→ 产品授权校验
  - `bind`：幂等键（**重放必须先于令牌校验**，否则第二次会因令牌已销毁而报过期，就不是「重放返回同一条」了）→ 冻结 → 单绑 → 令牌存在/未过期/摘要匹配 → 销毁令牌并写设备四维与事件与审计
  - 解绑：原因必填，恢复 `BOUND → ALLOCATED`，保留 `bindCount` 供识别反复解绑重绑
- **心跳与在线判定** `heartbeat_service`：`POST /device/heartbeat` **不使用 JWT**，以 `sn` + 设备密钥（SHA-256 摘要）鉴权；`online_status` 与 180 秒窗口口径统一到 `last_heartbeat_at` 单一事实来源
  - 新增 `POST /platform/devices/{id}/credentials` 签发 `DEVICE_SECRET`：明文仅回一次、库内只存摘要、续签覆盖同类型旧密钥（设备丢失时正是靠这个作废旧钥）——否则「任何人拿到 SN 就能把设备刷成在线」，而 SN 印在机身与包装上
  - 响应新增派生布尔 `online`；列表筛选与 `byOnlineStatus` 统计同样按窗口口径，避免「掉线了但枚举还写着 ONLINE」
  - `POST /platform/devices/{id}/simulate-heartbeat` 返回 `simulated: true`，`online=false` 可模拟窗口超时（否则验收要干等三分钟）
- **API**：新增 14 个端点 —— 平台端分配 5（列表/创建/详情/明细/执行）+ 设备 2（模拟心跳、签发密钥）+ 商户端 6（设备列表/详情/事件、绑定记录列表、precheck、bind、unbind）+ 设备侧 1（心跳）；`GET /platform/devices` 新增 `unallocated` 筛选（平台库存）；OpenAPI 快照 51 → **65 个路径**
- **前端**：平台端新增设备详情页（3 tab：概览 / 绑定 / 事件时间线）与分配管理页（新建弹窗含按租户联动的产品选择与设备多选、执行结果逐条展示失败原因）；商户端新增「我的设备」（两步扫码绑定弹窗，含预检回显与确认码倒计时）与「绑定管理」；设备库存页改造（新增「在线」列与「模拟心跳」）
- **演示数据**：种子新增 4 台**平台自有库存**设备（`tenant_id` 为空 + `IN_STOCK`）——订单生成的设备建时就带 `tenant_id`、批次导入的停在 `GENERATED`，没有这批数据时分配页的「选择设备」永远是空的
- **P-04 复核**：todo 中「修复 P-04（登录校验租户状态）」**已在 P1 修复**，本阶段复核确认（登录与刷新两条路径都校验 `tenant.status`，`test_auth.py` 已覆盖），未重复实现

**P6 烧录工厂端**

- **数据层**：Alembic `0012_factory_account_link` / `0013_device_factory_order`，与 ORM 零漂移、升降可逆
  - `user_accounts.factory_id`：`factories` 表从 `0002` 就存在、`factory_orders.factory_id` 也是 NOT NULL 外键，但账号侧**没有任何字段指向工厂** —— 工厂账号不知道自己属于哪家工厂，工厂端要么看不到任何数据、要么被迫看全部工单（多工厂时等于 A 厂能读到 B 厂的产量）。归属显式化后，工厂作用域过滤（`factory_scoped` / `assert_factory_visible`）才有依据
  - `devices.factory_order_id`：**设备与工单的归属落库**（ADR-09）。此前「这张工单负责哪几台设备」只能退化成「订单下状态为 X 的设备」，在派单那一刻正确、随后就会漂移（P6 验收实测到两个后果：二维码清单多打标签、抽检范围过宽）
- **状态机按业务能力放宽**（ADR-10，用户决策）
  - 冻结范围 `{IN_STOCK}` → `{IN_STOCK, ALLOCATED, BOUND}`，并**补齐 `ALLOCATED`/`BOUND → FROZEN` 的入边**：此前「已出货给商户的设备」无法停服（欠费停机、内容违规停用），绑定流程里的冻结校验也退化成不可达的防御性代码；解冻仍按 `previous_asset_status` **原路恢复**（已分配的设备不会退成平台库存）
  - 可分配范围 `{IN_STOCK}` → `{IN_STOCK, SHIPPED}`：接通 P4 起就存在却无处使用的 `SHIPPED → ALLOCATED` 边（迁移表允许、服务层拒绝的自相矛盾）
  - 可冻结集合与 `ASSET_TRANSITIONS[FROZEN]` 由单测断言**严格相等**，两侧必须同改
- **字段白名单脱敏** `app/services/serializers.py`
  - `FactoryOrderSerializer` 的 `ALLOWED_FIELDS` 与响应模型的 `model_fields` 由单测断言完全一致；序列化**只用显式传参构造**（不 `model_validate(order)`，否则 ORM 上新增的敏感字段会被自动搬运）
  - 脱敏后的客户名字段命名为 **`customerNameMasked`**：响应模型里根本没有 `customerName`，「忘了脱敏」在类型层面写不出来
  - `mask_customer_name` 取「首个/末个**文字字符**」而非字面首尾字符：`星辰玩具（演示租户）` 若按字面首尾会得到 `星********）`（括号占可见位、还漏出「后面跟着括号备注」）；现为 `星********户`，**长度恒等**（幂等性与全部不变量得以保持）
- **生产链路** `app/services/factory_service.py`：派单 → 烧录上报 → 抽检 → 出货登记，并**驱动设备四维与订单状态**
  - 派单：订单 `IN_STOCK → PRODUCING`、本单认领的设备 `IN_STOCK → PRODUCING`；工单 `quantity` = **实际派工台数**（合同数量另以 `orderQuantity` 下发）——取合同数量会让「烧录台数」与「实际出货设备数」永久对不上账
  - 烧录满额：工单 `COMPLETED`、设备 `PRODUCING → PRODUCED`、订单 `PRODUCING → SHIPPED_TO_CLIENT`；超量上报返回 `BURN_COUNT_EXCEEDED`(400) 并带 `remaining`
  - 出货：工单 `COMPLETED → SHIPPED`（终态）、设备 `PRODUCED → SHIPPED`
  - 抽检：SN 必须存在且**属于本工单**（判据是 `factory_order_id`，不是 `order_id`）；**刻意不改任何状态**——抽检不合格的真实含义是「需要人决定返工 / 降级」，自动打回工单只会把质量问题淹没成一个普通状态变化
- **入库端点**（P5 遗留②）：`POST /platform/devices/stock-in` —— 批次导入的设备停在 `GENERATED` 时进不了分配链路（分配只接受 `IN_STOCK`）。逐行判定（不存在 / 已入库跳过 / 其它状态失败）+ 幂等重跑 + 失败明细；平台端设备页提供行内「入库」与「批量入库」
- **API**：新增 15 个端点 —— 工厂端 9（`/factory/stats`、订单列表/详情/二维码、烧录/出货/抽检、抽检记录、固件版本）+ 平台端 6（`/platform/factories`、工厂订单列表/详情、派单、入库）；OpenAPI 快照 65 → **88 个路径**
  - 首次引入**工厂作用域收口** `factory_scoped` / `assert_factory_visible`（商户是租户维度、工厂是工厂维度，两者正交不可互替）；工厂账号未绑定工厂时直接 403，**不退化为「看全部」**
  - 越权访问刻意返回 404 而非 403，避免通过错误码差异探测他厂工单是否存在
- **前端**：工厂端 6 页（工作台接入真实统计 / 生产订单 / 工单详情（三 tab，页头动作随状态变化）/ 烧录上报 / 抽检确认 / 固件版本）+ 平台端「工厂订单」页；平台端订单详情新增「派单给工厂」（仅 `IN_STOCK` 时出现）、设备页新增「入库 / 批量入库」；冻结弹窗文案补「已分配给商户的设备冻结后即停服」
- **演示数据**：种子新增 1 家演示工厂（`FACTORY-DEMO-01`）并把工厂管理员账号绑上去（含对 P6 之前建好的库的**幂等回填**），否则工厂端登录得进去、什么都点不开
- **测试**：+281 条 —— `test_mask.py`（132：脱敏边界全表、不变量、白名单一致性）、`test_factory_desensitize.py`（7：脱敏红线，含平台端对照防止「数据本来就没有」的假通过）、`test_factory_flow.py`（42：全链路 + 跨厂隔离 + 归属范围 + 状态机驱动）；另**补齐 P5 欠账** `test_allocation.py`（33）、`test_tenant_isolation.py`（36，含路由元测试双向比对 `app.routes`）、`test_device_heartbeat.py`（31）

**P8 终端用户小程序端**

- **数据层**：Alembic `0014_miniapp_end_users_recharge`（`end_users` / `recharge_plans` / `recharge_orders` / `devices.settings`），与 ORM 零漂移
  - 这三张表**原属 P9 的 `0012`**，但 P8 的小程序登录与 4G 充值直接依赖它们（没有 `end_users` 就无法确定「谁绑了这台玩具」，P5 的 `device_bindings.end_user_id` 等的就是这张表）；按「落库先后顺延」P8 占 `0014`，**P9 顺延为 `0015`**
  - `end_users` **刻意不带 `tenant_id`**：一个家长可能买过两个品牌的玩具，把账号绑到某个租户会立刻产生「同一部手机在不同品牌下是两个账号」；终端用户的可见范围由**绑定关系**决定，收口点是绑定而不是租户
  - `recharge_plans` 按租户配置（流量是租户向运营商采购再零售的资费，不存在平台通用套餐）；订单**快照**金额 / 流量 / 有效期，套餐改价不回溯历史订单
  - `devices.settings` 用 JSON 而非逐项列：设置项随型号与固件迭代，逐项开列意味着每加一项就要一次迁移；**键白名单由服务层维护**，代价是约束不再由数据库强制（已在 docstring 写明）
- **终端用户令牌与后台令牌同密钥、以 `type` 区隔**（`access` / `end_user`），两个方向**在解签阶段**就互相拒绝：小程序令牌调管理端 401，反之亦然。比「各自维护一份密钥」少一个需要轮换的秘密，比「只靠作用域检查」不依赖业务代码记得检查
- **`EndUserContext` 与 `AuthContext` 分成两个类型**：终端用户没有角色 / 权限码 / 租户，复用同一结构意味着 `require_perm` / `scoped` / `assert_visible` 会跑在语义不成立的输入上；用不同类型可在编译期（mypy strict）挡住
- **短信与支付是可配置的模拟通道**（`MINIAPP_SMS_PROVIDER` / `PAYMENT_PROVIDER`）：`mock` 时验证码在响应里回显、支付直接置为成功，**并显式标注 `mock: true`**；`none` 时 503（安全失败）。**生产环境（`APP_ENV=production`）禁止 `mock`，启动期即拒绝**——与既有 DEBUG / SEED_DEMO_DATA / localhost CORS 同一处置方式
- **服务层** `miniapp_service`：验证码登录（摘要存储 + 尝试次数 + 过期作废）、扫码解析（`JX`/`JD` 双格式路由、不可绑定给 `reason` 而不报错）、双路径激活（`NOT_ACTIVATED → ACTIVATING → ACTIVATED`，厂商失败退回 `NOT_ACTIVATED` 并按 ADR-07 抛 503）、绑定与解绑（复用 `DeviceBinding` 同一行、`bind_count` 留痕）、设备设置（键白名单）、充值（套餐 → 下单 → 支付）
- **对话编排** `dialogue_service` + `app/realtime/ws_chat.py`：WS 帧协议（`session.open` → `session.ready` → `user.text`/`user.audio` → `asr.partial` → `assistant.delta` → `assistant.audio` → `assistant.done{messageId, latencyMs}` / `error` → `session.close`）与 **SSE 降级** `/miniapp/chat/stream`；会话与消息落库；令牌与设备走 **query**（浏览器 WebSocket 不能带自定义 header），非法令牌 close `4401`、越权 close `4403`
- **内容安全三开关**在 `assistant.delta` **之前**过滤：命中即整段替换为兜底文案、**原文绝不出站**，并写 `safetyFlag` / `blocked` 与 `CONTENT_BLOCKED` 审计；开关开启时牺牲首字延迟换取该保证，关闭时真流式
- **API**：小程序端 **16 个 HTTP 端点 + WebSocket + SSE**（`/miniapp/auth/code`、`/auth/login`、`/profile`、`/scan/resolve`、`/devices`、`/devices/{id}`、`activate-4g`、`activate-wifi`、`unbind`、`settings` 读写、`/recharge/plans`、`/recharge/orders` 与支付、`/chat/stream`）
- **前端**：把 P2 那个 720 行的 `main.js` 拆成 `shell.js`（外壳 + `mpApi` 终端用户 HTTP 客户端）+ **9 个屏幕模块**，全部换成真实接口；对话页用 `ChatSocket` 的**继承覆盖**接真实 WS（原因见下）
- **演示数据**：4G 客户产品 `CP-T001-4G`、t-001 的 3 个流量套餐、2 台「**已分配待激活**」演示设备（4G / Wi-Fi 各一台），以及把两个演示产品**显式配成离线模拟引擎**的 AI 配置——没有它，两条激活链路都会按 ADR-07 正确返回 503，演示不出流程本身（未配置的产品依然 503，安全失败行为仍可观测）
- **测试**：新增 `tests/integration/test_activation.py`（20 条），测试总数 737 → **757**

**P9 AI 配置与运营看板**

- **数据层**：Alembic `0015_ops_metrics_ota`（`metrics_daily` / `metrics_hourly` / `metrics_region` / `content_hot_ranking` / `content_items` / `ota_packages` / `ota_records`）+ `devices.region`（地域分布的数据源）+ `dialogue_messages.content_item_id`（内容热度榜的数据源）
  - 四张指标表都把 `client_product_id` 做进**唯一键**：遗留缺陷 P-08「运营数据全局共享」的根因就是聚合时丢了产品维度，让「漏掉它」在表结构上不成立
  - `content_hot_ranking` 存**标题快照**：内容下架或改名后，历史排行仍显示当时的名字，复盘不会看到空标题
- **对象存储抽象** `app/core/storage.py`：`LocalStorage`（本地磁盘，目录结构即 key 层级）+ `S3Storage`（**显式安全失败**，不假装可用）；`_safe_key` 是**唯一的路径拼接入口**（拒绝绝对路径、`..`、空段），杜绝原型的路径穿越漏洞（附录 B 同源问题）
- **商户端 AI 配置**（26 个端点）：供应商 / 提示词与采样参数 / 角色 / 音色 / **内容安全三开关**，**分区独立保存**；`temperature` 库里存 ×100 整数、出入参换算 0–2 小数
  - `/role` 对 Wi-Fi 产品 409：**Wi-Fi 方案的对话角色由厂商侧智能体配置，平台不覆盖**（`roleSupported` / `roleUnsupportedReason` 一并下发，前端直接禁用该分区）
  - 供应商清单**只回「是否已配置」布尔，绝不含密钥或片段**——延续 P3 的密钥保护口径
- **知识库**：CRUD + 文件上传/删除/解析（走存储抽象），删除前校验是否被 `ai_configs` 引用（`CASCADE_CONFLICT` + 列出引用产品）
  - 解析**诚实实现**：文本类按段落切块并统计块数；pdf/docx 返回 `FAILED` + 「文本抽取尚未实现」——**关键：`PENDING` 与 `PARSED` 是两个独立事实**，上传成功不等于能被检索命中
  - 知识库**真的影响对话**：`dialogue_service` 把 `PARSED` 文件的文本块作为 `context["knowledge"]` 注入供应商（离线引擎据此改写回复）
- **运营指标**（7 个端点）：概览**实时聚合**（`source: live`）／趋势、24 小时、地域、内容榜**读快照**（`source: snapshot`）——两条路径在响应里显式标注，避免「这个数是怎么来的」无人能答
  - ★ **P-07 的修复**：所有维度数据来自真实 `GROUP BY`，**没有任何系数摊派**；核心守卫是「快照之和 == 实时聚合」这条测试断言
  - ★ **P-08 的修复**：所有端点强制 `productId` 且校验归属（传他人产品 → 404）；两个产品共用一个租户时指标互不串台
  - 留存（D1/D3/D7/D30 / 流失设备 / 回访率 / 平均间隔）：**分母为 0 时比率为 `null` 而不是 0**——不把「未知」伪装成「零」
  - 内容榜的归属来自 `ChatChunk.content_title`（**供应商声明**用了哪条素材），不做「回复文本里出现《标题》」式文本嗅探（会被用户自己说出的书名与角色前缀污染）
- **OTA**（8 个端点，**仅平台端**）：固件包登记/上传/删除 + 逐台推送记录（`PENDING → PUSHING → SUCCESS|FAILED`）
  - ★ 推送能力判定读 `cloud_providers.ota_support`——**数据驱动**而非写死厂商：Wi-Fi 方案（JoyInside / 火山）→ 409 `OTA_NOT_SUPPORTED`，`details` 带 `{otaSupport, cloudVendor, cloudProviderName}`，前端据此给出「这不是设备选择错误，换成其它设备也不会成功」的提示
  - 推送成功才更新设备 `firmware_version`；已是最新版本计 `skipped`（幂等）；**被拒绝时不留任何推送记录**（「拒绝」必须是真的没发生）
  - **无文件的固件包逐台 FAILED 并说明原因**——ADR-07「绝不伪造成功」在 OTA 上的落点
- **前端**：商户端 5 页（我的产品 / 产品详情 / AI 配置 / 知识库 / 运营看板）+ 平台端 OTA 页 + 客户端产品详情追加「运营」Tab；`chart.js` 复用 P2 已交付的柱/折线/环形/热力/迷你趋势/堆叠条
  - AI 配置页底部有「**当前生效说明**」：把三个安全开关翻译成设备实际行为（例如「命中敏感词时整段替换为安全文案，原文不会下发出设备」）
- **演示数据**：内容库 6 条（标题与离线引擎素材同名，否则内容榜聚合不出来）、终端用户 3 个、**确定性对话历史 13 会话 / 72 消息**（两个产品规模刻意不同——这正是 P-07/P-08 验收的前提）、固件包 1 个、11 台设备带地域
- **测试**：+46 条 —— `test_metrics_isolation.py`（9）、`test_ota.py`（14）、租户隔离矩阵扩到 26 条 P9 商户路由（首次覆盖「查询参数形态的越权」）

**P10 部署与质量保障**

- **编排与镜像**：`docker-compose.yml`（仓库根）三个 profile —— 默认（app + SQLite）、`nginx`（前置反向代理）、`postgres`（可选数据库）；`env_file` 用 `required: false`，缺 `.env` 也能起；`deploy/Dockerfile.backend` 多阶段构建、非 root 运行，CMD 改为 `alembic upgrade head && python -m app.db.seed && uvicorn … --workers ${WORKERS:-4}`（迁移与播种在 fork 之前完成一次）
  - 编排文件放**仓库根**而非 `deploy/`：compose 的三个相对路径（`build.context`、`env_file`、`${VAR}` 插值读的 `.env`）都以 compose 文件所在目录为基准，放根目录时三者自然成立
- **新增容器专属配置项**（解决「开发机的 `.env` 直接搬进容器就坏」）：`APP_DATABASE_URL`（与开发用的 `DATABASE_URL` 分开，默认绝对路径 `sqlite+aiosqlite:////data/toyverse.db` 且挂在卷上）、`DATA_DIR`、`FRONTEND_DIR`、`WORKERS`、`SEED_AT_STARTUP=false`
- **`app/core/config.py` 适配容器布局**：`_guess_repo_root()` 代替「`__file__` 上溯固定级数」；`_resolve_frontend_root()` 支持 `FRONTEND_DIR` 显式覆盖；`DATA_ROOT` 可由 `DATA_DIR` 指定；`ensure_runtime_dirs()` 把 `PermissionError` 转成**可操作的** `InsecureConfigurationError`（而不是崩溃循环）
- **`deploy/nginx.conf`**：修正 WebSocket location（真实路径是 `/ws/miniapp/chat`，**不在 API 前缀下**）、对**未指纹化**的 JS/CSS 改用 `no-cache, must-revalidate`（图片/字体仍给 30 天）、补 `location = /login`（它是应用路由不是磁盘文件）、逐端 SPA 回退（`/platform/` `/merchant/` `/factory/` `/miniapp/` 各自回退到自己的 `index.html`）
- **`scripts/smoke_test.sh`**：全端点冒烟（**57 项断言**），按真实角色登录后逐端点请求、以只读为主（例外是两个幂等写），带**最低断言数守卫**（断言数为 0 时判定脚本异常，而不是打印「全部通过」）
- **`scripts/scan_secrets.py`**：敏感信息扫描四类规则 —— 弱口令 / 硬编码密钥 / **出参模型里的明文密钥字段** / 敏感文件入库（`.env`、`data/`、`learning/`）；扫描被跟踪文件 **0 命中**（当时 236 个；P11 新增截图与测试后为 245 个），并带一个「规则确能命中」的自测
- **`scripts/gen_qrcodes.py`**（演示二维码清单：PNG + CSV/JSON manifest，载荷经 `qrcode_service` 单一实现）、**`scripts/reset_db.sh`**（先备份到 `data/backups/<时间戳>/`、需二次确认、`--yes` 供自动化）、**`scripts/dev.sh`**
- **`.dockerignore`**：构建上下文从 229MB 降下来，并杜绝 `.env` 被 `COPY . .` 带进镜像
- **`.github/workflows/ci.yml`**：五个并行 job —— lint / mypy + test / 契约快照 / 前端导入契约 / 敏感信息扫描；镜像构建**刻意不放在 CI**（pip 全量安装是分钟级，理由写在注释里）
- **`tests/e2e/test_full_loop.py`**（本阶段提前交付，里程碑 M2 的自动化验收）：一条测试串完主干 —— 客户开通 → 云服务商 → 产品模板 → 授权 → 客户产品 → 商户下单 → 平台审核 → 生成设备（Wi-Fi 本地 SN）→ 入库 → 派单工厂 → 烧录上报 → 抽检 → 出货 → 分配（`SHIPPED → ALLOCATED`）→ 终端用户登录 → 扫码解析 → 激活并绑定 → SSE 流式对话 → 会话与消息落库 → 设备四维终态与七种时间线事件
  - 逐步断言而非只断结果：任一环断裂都能从断言消息定位到具体环节；含脱敏红线（工厂端响应不得出现客户名 / 联系方式 / 金额字段）
  - 之所以提前做：它验证的是 P0–P8 九个阶段的成果，与 P9 无关，且是「M2 业务闭环」这个里程碑唯一的客观凭据
- **`Makefile`**：修正 `up`（原指向不存在的路径），新增 `up-nginx` / `up-full` / `smoke` / `reset-db` / `qrcodes` / `fe-check` 等目标

**P11-A 文档一致性门禁**

- 新增 `scripts/check_docs.py` 与 `make docs-check`：把「文档悄悄过期」从**人的记忆问题**变成**红灯**，
  与既有的 `make fe-check`（前端导入契约）、`make openapi-check`（接口契约）同一套思路。**八类校验**：
  1. `docs/06` 端点表与 OpenAPI 快照**双向**比对（方法与路径），并校验文档声明的端点总数与 `paths` 数
  2. `docs/05` 字段字典的表名集合与迁移/模型一致（脚本内还交叉校验「迁移集合 == 模型集合」，两者不一致直接失败）
  3. `docs/06` 错误码与 `ErrorCode` 双向比对，**并校验文档写的 HTTP 状态与 `_STATUS_MAP` 一致**
  4. `docs/07` 权限码与 `permissions.py` 双向比对（54 个）
  5. `docs/05` 状态枚举小节的取值与 `enums.py` 成员比对
  6. 文档引用的 `make <目标>` / `scripts/<文件>` / 相对文档链接是否真实存在
  7. `docs/11` 的**逐文件用例数**与源码一致（并校验 unit / integration / e2e 三个分组的文件数与函数数）
  8. 每份 `docs/NN-*.md` 是否含「已知局限 / 取证边界」小节；是否引用了不上传目录的内部路径
- 接入 `make check`（现为 `lint + typecheck + test + openapi-check + docs-check`）与 CI 的 `contract` job
- ★ **首次运行就报出 5 处问题**：`docs/11` 的 `test_allocation.py` 用例数写错（33 → 实际 23，会让分组合计对不上 353）、
  `docs/14` 缺边界声明小节、`docs/07` 把不存在的权限码写进反引号，以及**两处检查器自身规则过严导致的误报**。
  → 最后一类的处理值得记下：**修订规则以匹配真实风险（断链），而不是让文档内容为迁就工具而变形**

### 修复

**P11 公开前整理**

- ★ **PostgreSQL 上运营指标两个端点直接 500**（**SQLite 上永远复现不出来**的缺陷）：
  `metrics_service.py` 的 `_date_between` 把日期用 `.isoformat()` **当字符串绑定**——
  `func.date(col).between(date_from.isoformat(), date_to.isoformat())`。
  SQLite 是动态类型，`func.date()` 返回 TEXT，字符串比字符串完全正常（804 个用例全绿）；
  但 PostgreSQL 的 `func.date()` 返回真正的 `date`，与 `varchar` 比较直接抛
  `asyncpg.exceptions.UndefinedFunctionError: operator does not exist: date >= character varying`，
  导致**运营看板的概览与快照重建两个最核心的端点 500**。
  修复：绑定 `date` 对象而不是字符串（两种方言都正确——SQLite 方言渲染成 ISO 字符串，
  PG 方言绑定为 `date`）。**发现方式：公开前真跑一次 PostgreSQL，冒烟报出这 2 项失败。**
- 新增回归守卫 `tests/unit/test_sql_dialect_portability.py`（**3 个函数 / 5 个用例**）：
  **不测行为、测绑定参数的类型**——用 PG 方言编译 `_date_between`，断言所有绑定参数都是
  `date` 而非 `str`。已实测该断言在旧实现上会失败（旧写法绑 `str` 被拦下），因此它真的能防回归。
- **CI 的两处修正**：① `pytest -q` 本就会收集 e2e，而后又单独跑了一次 → 首次改为
  `--ignore=tests/e2e`（**此前每次 CI 都白等约 5 分钟**）；② 新增**覆盖率下限**
  `--cov-fail-under=65`——本项目到 P11 才发现「804 个用例全绿但覆盖率只有 70%」，
  而当时**没有任何机制会提醒**；下限取 65（实测 70，留 5 个点余量避免环境抖动）
- **README 快速开始补全 6 项密钥**：启动期安全校验要求 `JWT_SECRET_KEY`、`QR_SIGN_SECRET` 与
  4 个管理员口令，而 README 原文只让改 3 个管理员口令——**在干净目录里照字面操作 `make setup` 直接失败**。
  已列出全部 6 项并附可选的一行脚本（发现方式：装作第一次来的用户，照着 README 做一遍）
- **重新采集 14 张截图**：原截图采于「账号中立化」之前，登录页的演示账号按钮显示的是
  **旧商户账号**（该按钮是 `frontend/index.html` 里硬编码的，代码改了而图没改）。
  已换新账号 `13812345679` 重采，并加了**机器验证**：用 `--dump-dom` 抓 14 个页面的渲染 DOM，
  逐页 grep「不应出现的字符串」（旧账号 / 验收痕迹 / 已移除的内部文件名）——**不靠肉眼看图**
- `docs/12` 三处过期表述（「M5 未达成」「截图未做」「README 未终版」）已同步
- **敏感信息扫描器两处规则过宽**（扫描自身也报了 8 处误报，均为假警报）：
  ① `.github/workflows/ci.yml` 的 4 处 **CI 专用占位口令**（`Ci-Only-Str0ng#Pass1` 等）
  被当成硬编码密钥——它们只存在于 Actions runner 内、不指向任何真实环境，且**自我声明为 `ci-only`**；
  ② `scripts/smoke_test.sh` 的 4 处 `TOKEN="$(login ...)"` 这类**命令替换**被当成字面量。
  修法：把 `ci-only` 加入占位符标记（**按值排除**而不是按文件加入 allowlist——后者会让**整个文件**
  不再被扫描），并让规则跳过 `$(` / 反引号开头的命令替换。
  ★ **并验证了收敛后的规则仍然会响**：真实密钥形态（`"aB3xK9mQ2pL7wZ4tR8yN6vC1"`）仍命中、
  一个含真实密钥的临时文件端到端仍报出 1 条 —— **「规则不响」与「规则坏了」必须能区分开**。

**P10 部署与质量保障**

以下问题**全部是「写好了但真跑起来不通」的类型**——没有一个是靠读代码或跑集成测试能发现的。可部署性只能靠真的部署一次来证明。

- **`make up` 从 P0 起就是坏的**：Makefile 写 `cd deploy && docker compose up`，而 compose 文件在仓库根。只跑 `make test` 不会执行 Makefile 的 docker 目标，因此一直没暴露。改为在仓库根执行并拆出 `up-nginx` / `up-full`
- **nginx 的 WebSocket 路由错**：location 写成 `/api/v1/ws/`，真实路径是 `/ws/miniapp/chat`（小程序 WS **不在 API 前缀下**）→ 握手落到 SPA 回退、拿回一段 HTML。本地不经 nginx 时一切正常。改为 `location /ws/`，并用真实 WS 握手验证：成功建立后以 `4401`（非法令牌）关闭，而不是返回 HTML
- **nginx 对未指纹化的 JS/CSS 声明 `immutable` 强缓存 1 年**：本项目前端文件名不带内容哈希，发版后用户会卡在旧代码且**无法失效**。改为 `no-cache, must-revalidate`；图片/字体仍给 30 天
- **`DATABASE_URL` 把开发机相对路径泄漏进容器**：`${DATABASE_URL:-…}` 从宿主机 `.env` 插值出相对路径，SQLite 因此落在**镜像层**而非卷上，`down && up` 丢数据（不重建容器时看不出差别）。引入容器专属 `APP_DATABASE_URL`，默认绝对路径 + 挂卷
- **缺 `.dockerignore`**：构建上下文含 229MB 的 `.venv`，且 `.env` 有被 `COPY . .` 带进镜像的风险（构建慢与泄漏都只在构建时暴露）
- **`POSTGRES_PASSWORD: ${…:?}` 让整个 compose 不可用**：compose 的变量插值发生在 profile 判定**之前**，因此没配 PostgreSQL 口令的 `.env` 会让整套编排直接报错。改为 `${POSTGRES_PASSWORD:-}` 并加注释说明失败仍是安全的（postgres 镜像本身拒绝空口令）
- **`--workers 4` 下每个 worker 各播种一次**：4 个进程并发写同一 SQLite（实测 1 成功 3 失败），且赢得竞争的 worker 可能只写一半。改为 `SEED_AT_STARTUP=false` + 在 CMD 里 fork **之前**播种一次
- **`FRONTEND_ROOT` 在容器里推算到 `/`**：原按 `__file__` 上溯三级，而容器布局没有 `backend/` 层 → **整个 UI 404 而 API 完全正常**，日志只有一条 WARNING（本地布局恰好是三级，所以一直没暴露）。新增 `_guess_repo_root()` / `_resolve_frontend_root()` 与 `FRONTEND_DIR` 显式覆盖
- **`DATA_ROOT` 不可配置 + 进程非 root**：容器启动即 `PermissionError: /app/data` 崩溃循环（本地开发目录可写，看不出问题）。新增 `DATA_DIR`，并把 `PermissionError` 转成**可操作的** `InsecureConfigurationError`
- **nginx 缺 `/login`**：它是**应用路由**而不是磁盘文件 → 经 nginx 访问登录页 404（直连时由 FastAPI 提供）。加显式反代；因配置是 bind-mount，改后需 `docker compose restart nginx`
- **`smoke_test.sh` 的 `json_get` 永远取不到值**：用 heredoc 把程序喂给 `python3`，同时响应体也在 stdin → 程序与数据争同一个 stdin，取值恒为空，于是**登录永远判失败**（而同一口令用 curl 直连是成功的——这个对照正是定位的突破口）。改用 `python3 -c` 传程序，把 stdin 留给数据
- **`smoke_test.sh` 在误删计数函数后仍打印「0 通过 / 全部通过」**：**会静默通过的检查比没有检查更危险**。恢复 `ok` / `bad` / `skip` 三个计数器并加 `MIN_EXPECTED_CHECKS=25` 守卫
- **`scan_secrets.py` 首版 133 条误报**（规则太宽，而非真有泄露）：手机号 `13812345678` 命中「8 位连续数字」弱口令规则、HTML 的 `placeholder=` 被当成密钥赋值、黑名单表里的枚举字面量、`*SCREAMING*` 常量、URL 路径常量，以及请求 DTO 里**本来就要收明文**的 `access_key`。收窄弱口令表并只匹配**赋值形态**、给测试目录加 `ALLOWED_PATH_PREFIXES`、新增 `_looks_like_a_real_secret()`、把「明文密钥字段」规则限定到**响应类**；收敛后 0 命中，并补自测证明规则**确实会命中**——**「规则不响」与「规则坏了」必须能区分开**

**P9 AI 配置与运营看板**

- **24 小时分布聚合错误**（真缺陷，会造成看板数字悄悄错）：`rebuild_metrics` 复用了「聚合基座」`_message_base`（`select(func.count())` 起手）再叠加行级列 `created_at`，生成 `SELECT count(*), created_at FROM ...` —— **没有 `GROUP BY`**，SQLite 因此只返回**一行**（count=6、`created_at` 取自任意一条），于是小时分布被算成「某个小时 1 次」而不是「9 点 3 次、14 点 3 次」。不会报错、总量还勉强能对上，因此极易漏过。修复：需要逐行数据时必须显式 `select(...).select_from(...)`；并给 `_message_base` 补了「⚠️ 只用于聚合」的警告 docstring
- **同一路径两个处理函数**（真缺陷，架构级）：P4 的 `merchant.py` 与 P9 的 `merchant_ops.py` 都定义了 `/merchant/products` 与 `/merchant/products/{id}`，最终生效取决于 `router.py` 的 include 顺序——「改了别处的 import 顺序就会静默换实现」。修复：收敛为一个**富化版本**（列表也带设备计数与 AI 摘要，避免前端逐行回查详情造成 N+1），删除 P4 版并在原处留下说明
- **演示数据时间戳落在未来**（真缺陷，演示数据）：种子把「今天」的会话放在 09:00/14:00，凌晨（UTC）运行时这些 `created_at` 属于未来，按 `created_at <= now` 的聚合会把它们排除，「今天」在图表上恒为空。修复：对 `day_offset == 0` 的小时做钳制，保证落在已经过去的时刻
- **三处文案写死厂商名**：角色不支持原因与 OTA 弹窗两处文案点名「京东 JoyInside」，而演示的 Wi-Fi 产品实际用火山引擎——判定本就数据驱动（`ota_support` / 联网方式），文案却写死了具体厂商，换供应商后就成了误导。改为只描述规则、不点名厂商

**P8 终端用户小程序端**

- **「已激活但未绑定」时重复激活只回放结果、不补绑定**（浏览器实测发现的真缺陷）：用户扫码激活时界面显示「激活成功」，但库里没有绑定关系——一进对话页就被 WebSocket 以 `4403` 拒绝（「这台设备不属于当前账号」），且界面上看不出原因。触发场景很常见：设备在别处激活过，或上一位用户**解绑**过（解绑**刻意不改变** `activation_status`，所以「已激活 + 未绑定」是合法状态）。修复：幂等分支改为**同时保证绑定关系**（已绑当前用户则空操作、未绑定则补绑、绑给别人则 409）；`_bind_to_end_user` 增加「同用户已绑定 → 空操作」的早返回，否则 `bind_count` 会被重复激活灌水，而它的用途正是识别反复解绑重绑的异常设备
- **既有 flaky 测试**（约 1/16 概率假通过）：`test_binding.py` 的「篡改 JD 签名末位」用例写作 `payload[:-1] + "0"`，而末位本身就是 `"0"` 时等于没篡改。改为显式翻转末位并断言「改动确实发生」——一个会随机不变换的用例比没有用例更糟

**P6 烧录工厂端**

- **工单二维码清单返回订单下全部设备**：工单委托 3 台（订单里另 1 台被冻结、1 台已先分配给客户），清单却返回 5 条 —— 工厂会多打 2 张标签，而标签一旦贴上就很难挽回。根因是把「归属」当成「订单 + 状态的函数」；改为按 `devices.factory_order_id` 筛选（迁移 `0013`）
- **抽检范围过宽**：只比对 `order_id` 相等，一台**从未进入生产**的同订单设备也能被这张工单抽检并计入合格率（污染质量口径）。改为比对 `factory_order_id`，界面提示同步说明「SN 必须属于所选工单」
- **平台端设备页的冻结按钮点不到已分配设备**：后端已放宽到可冻结 `{IN_STOCK, ALLOCATED, BOUND}`，前端 `FREEZABLE` 集合却还是 P5 的 `['IN_STOCK']` —— 能力在 API 层可用、在 UI 层不可达。集合同步并在注释里留下两次漂移的教训（P4→P5、P5→P6 是同一个坑）
- **工单「烧录台数」与实际出货设备数对不上账**：`quantity` 原取订单合同数量，订单 2 台、其中 1 台被冻结时，工单写 2 台却只有 1 台会流转，报满后 `burned_count(2) > 出货设备(1)`。改为取**实际派工台数**并另下发 `orderQuantity`，两个数字都可见、差异可对账
- **`FACTORY_ORDER_EXISTS` 正常流程不可达**：状态校验排在重复派单检查之前，而首次派单已把订单推到 `PRODUCING`，于是第二次派单只得到笼统的 `INVALID_STATE_TRANSITION`，前端拿不到 `existingFactoryOrderNo` 去跳转。调整校验顺序（重复派单是本端点最高频的误操作，必须在最前面说清楚）
- **脱敏结果被包裹性标点占据可见位**：`星辰玩具（演示租户）` → `星********）`，工厂端看起来像坏数据，还漏出「这个名字后面跟着一段括号备注」。改为取「首个/末个文字字符」，长度不变、全部不变量保持
- **三处注释 / OpenAPI 描述与实现不符**：`DeviceHeartbeatResponse.online` 写「恒为 true」（`simulate-heartbeat` 传 `online=false` 时为 false）、冻结端点写「仅 `IN_STOCK` 可冻结」、`_resolve_thaw_target` 写「`previous_asset_status` 必然是 `IN_STOCK`」——两处会进 OpenAPI 描述，已重生成契约快照

**P5 设备 / 分配 / 绑定**

- **所有页头按钮显示成字面文本 `<span>刷新</span>`**（P2 遗留）：`shared/ui/components.js` 的 `button()` 嵌套 `html` 模板时漏了 `raw()`，整段被二次转义；P4 的设备页同样中招，此前验收未发现。已补 `raw()` + `esc(label)`
- **分配单弹窗选完租户后「可选设备」永远为空**：租户 `change` 处理把设备列表清空却没重新加载，而「先选租户」是必然动作 —— **分配功能实际完全不可用**。可选设备是「平台库存中已入库且未分配给任何租户」的那批，与目标租户无关，因此只清勾选、不清列表
- **设备详情时间线的事件明细显示字面 `<div>…</div>`**：调用方按「body 是 HTML」传参，而 `timeline()` 的契约是纯文本，被二次转义。改为调用方传纯文本并给 `.timeline-body` 补 `white-space: pre-line`；**刻意不把组件改成 `raw`**——body 含用户填写的解绑原因，组件转义是必要的 XSS 防线
- **签发设备密钥后详情页不刷新**：凭证区块仍显示「尚未签发设备密钥」，用户会误以为签发失败；且弹窗打开期间背景表格显示的是**上一轮的掩码**，看起来像「弹窗里的密钥与页面上不是同一个」。已把 `reload()` 挪到弹窗之前（先刷新、再弹窗）
- **空白串可当「原因」落库**：`BindingUnbindRequest` / `DeviceFreezeRequest` / `DeviceRetireRequest` 只有 `min_length=1`、不 strip，`"   "` 能过关并写进记录，时间线里出现「解绑设备 X：   」使责任追溯失效（P4 的 `rejectReason` 已是「空白不算填了」口径）。三处都加 strip 校验器 → 400 `VALIDATION_ERROR`
- **二维码前缀大小写导致自相矛盾**：`parse_payload` 用 `.upper()` 容忍 `jd|`（P4 单测已钉死该宽容行为），但 precheck 的 SHA-256 命中按字节严格比对，于是同一份输入先被判「格式合法」再被判「伪造」，报错文案误导排查。现只把**前缀段**归一为大写，其余字节仍严格比对（前缀是格式标签、不承载安全语义）
- **工作台阶段说明文案过期**：平台端仍写「P3/P4 已交付」、商户端仍写「当前处于 P2 前端地基阶段：商户端接口将在后续阶段交付」，与已交付的能力不符，已更新到 P5
- **P-06（删除无级联校验）**：租户 / 云服务商 / 产品模板 / 授权的删除端点先校验关联数据，存在时返回 `CASCADE_CONFLICT` 并在 `details` 中给出各项数量
- **P-03（新建产品未绑定客户）**：客户产品的 `tenant_id` 为 `NOT NULL`，创建时必须指定租户与模板且校验授权（`PRODUCT_NOT_AUTHORIZED`），租户详情可直接看到其产品
- **前端列表页事件监听器累积**：`createListPage` 的事件委托原绑在应用壳长期持有的容器上，跨导航不断叠加，表现为「点一次按钮弹出多个弹窗」；改为绑定到每次渲染新建的根节点
- **`updated_at` 导致异步惰性加载异常**：原用 SQL 侧 `onupdate=func.now()`，UPDATE 后属性被标记过期，紧随其后的 ORM 序列化触发 `MissingGreenlet`（5 个 PUT 接口 500）；改为 Python 侧 `default` / `onupdate`，同时保留 `server_default` 供非 ORM 路径
- **模板详情字段缺失**：`GET /platform/templates/{id}` 未装配 `cloudProviderName` 与两个计数，与列表接口不一致；新增 `get_template_detail()` 并让创建 / 详情 / 更新统一走它
- **错误码契约不一致**：模板 / 云服务商 / 客户产品编码重复原返回 400 `VALIDATION_ERROR`，与既有 409 约定不符
- **`utcnow()` 前向引用**：`TimestampMixin` 引用 `utcnow` 而定义在其后，导入期 `NameError`

- **厂商失败的审计口径与可检索性**：4G 生成因厂商未配置失败时原写 `GENERATE_DEVICES`，导致 `VENDOR_CALL_FAILED` 查不到；现按失败根因分类，并给 `record_failure()` 补 `resource_type` / `resource_id`，失败审计可按订单反查
- **批次文件错配**：同一份 CSV 用于另一个订单时原会静默复用旧批次，导致导入的设备挂到旧订单/旧租户；现校验订单一致性，不一致返回新增的 `BATCH_FILE_CONFLICT`(409) 并给出批次号与订单号
- **状态相关的页头动作不随状态更新**：订单详情页的页头按钮只在首屏算一次，审核通过后仍显示「审核通过/驳回」，必须手动刷新才出现「生成设备」；`createDetailPage` 新增 `setActions()` 并在重载时按新状态重算
- **`api.raw()` 形同虚设**（P2 遗留）：`request()` 形参里没有 `raw`（仅写在 JSDoc 中），且即便补上也会先消费 body 使调用方 `blob()` 抛 `Body is unusable`；现 `raw` 时跳过 body 消费，`api.raw()` 自身隐含该选项
- **冻结语义不对称**（P1 遗留）：原允许冻结 `GENERATED` 但解冻只能回到 `IN_STOCK`，「恢复原状态」无法成立；收紧为**只允许冻结 `IN_STOCK`**，并新增 14 条单元断言把该决策钉死
- **批次字段上限与数据库列长不匹配**：原统一 128 大于 `sn String(64)` 与 `imei/iccid/mac String(32)`，换 PostgreSQL 会写库报错；改为按字段各自上限校验
- **`duplicatedRows` 口径**：原把「库内已存在」与「文件内重复」合并计数，现只统计文件内重复（库内冲突计入无效行）
- **冻结端点文档过期**：OpenAPI 文案仍写「GENERATED / IN_STOCK 可冻结」，已与实现对齐
- **批次读路径租户收口**：`error-report` 与 `import` 未走 `assert_visible`，破坏 ADR-08 读路径统一收口的不变式，已补齐
- **「审核通过」确认框文案**：沿用了驳回流程的「驳回是不可逆的」说明，已改为符合通过语义

### 文档

- 新增 `docs/13-火山引擎硬件对话智能体配置说明.md`（控制台实测 + 官方文档取证，含取证边界声明）
- 新增 `docs/14-ESP32-S3刷机与联调步骤清单.md`（真机联调当天的可执行清单：四条接入路径决策树、首次对话判据、失败速查 F-01~F-10）：≥60% 内容来自**控制台实测**（账号、免费额度、已有产品与智能体）与官方文档正文；明确标注取证边界（《获取开发凭证》《调用方法》两页正文不可读，签名算法待校对）
- 新增 `deploy/.env.example`：**生产部署清单**（不是开发模板的副本）——必须修改的项、「不安全就拒绝启动」的三类硬校验、容器编排专属变量（`APP_DATABASE_URL` / `DATA_DIR` / `FRONTEND_DIR` / `WORKERS`）、部署后自检与回滚步骤
- 新增 `.dockerignore` 与 `.github/workflows/ci.yml` 内的注释说明（为何镜像构建不放在 CI 里、各 job 的取舍）

**P11-A 专业文档集（13 份新文档）**

- 新增 `docs/README.md`：文档导航——**按「你想做什么」指路**（不是按文件名罗列），含三条推荐阅读路径，以及「文档与代码的一致性如何保证」一节
- 新增 `docs/01-安装部署指南.md`：环境要求 / 本地三步 / Docker 三种 profile / **11 大类 80 个环境变量的完整表格** / **启动期拒绝启动的 9 类条件** / 迁移与种子 / 升级与回滚 / **故障排查（9 条本项目实际踩过的坑）** / 生产加固清单（分「必做 / 强烈建议 / 未实现需自行补齐」三档）
- 新增 `docs/02-系统流程图.md`：**全部 mermaid**——全局业务闭环、订单状态机（9 态）、设备四维状态机（资产 10 态）、激活分支（`JX` vs `JD`）、批次导入、分配与绑定（含 precheck/bind 两步与四个刻意为之的顺序）、对话时序
- 新增 `docs/03-系统架构图.md`：六层架构 / 依赖方向约束 / 三种 profile 的部署拓扑 / 容器内布局（含 `/app/app` 双层结构这一踩坑点）/ 多租户隔离链 / 数据流原则 / **技术选型理由与替代方案对比表**（含「为什么不选 Vue + Vite」「为什么没有 Redis」）
- 新增 `docs/04-产品需求文档PRD.md`：产品定位与**非目标** / 6 角色 + 终端用户 / **24 条术语表** / 页面清单（含 **13 个未实现菜单项**的明示）/ 三处刻意能力收窄的说明 / **7 条界面设计原则**（每条都有真实依据）/ 非功能需求 / 版本变更。★ 明确标注「**本 PRD 为事后补写**」
- 新增 `docs/05-数据模型与ER图.md`（**1809 行**）：8 张分域 ER 图 / **46 张表 × 655 个字段的完整字段字典**（★ **由 ORM 元数据程序化导出**，非手抄）/ 六条复合唯一键的业务含义（含「指标表唯一键里为什么必须有 `client_product_id`」）/ 20 条复合索引的服务对象 / 全量状态枚举 / ★ **`in_stock` 语义对齐**（订单维度 vs 设备维度、与参考实现的差异、以及一条自检规则）
- 新增 `docs/06-API接口文档.md`：通用约定（认证 / 响应契约 / traceId / 幂等 / 分页 / **越权为何返回 404 而非 403** / UTC 时间口径）/ **31 个错误码表（含 HTTP 状态）** / **145 个端点表**（★ 由源码 AST 与契约快照**联合生成并双向断言**）/ WebSocket 帧协议与关闭码 / 三种传输通道的分工（WS / SSE / NDJSON）/ 9 组请求响应示例 / OpenAPI 契约门禁
- 新增 `docs/07-多租户与权限设计.md`：租户模型的三种 `tenant_id` 语义 / 工厂为何是**正交的第二维度** / 隔离七层链路 / 强制作用域实现（含**工厂为什么需要两层保护**）/ **54 个权限码完整表格** / 6 角色矩阵与三处刻意收窄 / 越权测试策略（参数化矩阵 + **路由元测试**）/ 脱敏规则（白名单 + 文字字符取值）/ 令牌双类型隔离
- 新增 `docs/08-AI能力接入设计.md`：能力矩阵 / `AIProvider` 接口分组 / ★ **为什么 `chat()` 必须是 `AsyncIterator[ChatChunk]`**（把传输形态差异关在接口内）/ **四层解析顺序**与显式的厂商映射 / 离线引擎（意图分支、关键词表、4 个内置角色预设、可复现性）/ 知识库**真的影响对话**的正反证据 / 4G 与 Wi-Fi 传输差异 / 火山与百度接入指引 / 密钥管理三约束 / ★ 诚实标注**四家适配器全部未联调**
- 新增 `docs/09-系统交付标准.md`：八道门禁与客观凭据 / **约 90 条逐条可勾选的功能验收清单**（每条给「验收动作 + 可观察结果 + 依据」）/ 性能 SLO（**明确区分实测与设计目标**，不编造 QPS/P95）/ 安全基线（**已实现 vs 未实现两栏**，后者原样承接 `SECURITY.md`）/ 兼容性 / 文档完备度 / **缺陷等级定义**（含「本项目对『严重』的一个不同判断」）/ 验收流程与记录模板
- 新增 `docs/10-AI验证可用性.md`：验证目标（含明确排除项）/ 测试环境 / **24 用例数据集**（7 类意图）/ 指标定义（★ **两栏意图判定**：引擎判定 vs 回复分支）/ **实测基准数据表**（安全拦截 6/6、引擎行为符合设计 22/24、产品视角满足 19/24、TTFT 中位 184.1ms、整段中位 365.1ms、分块 2/6/20）/ 逐用例明细 / 可复制的复现命令 / 结论与 **10 条局限**（★ 首要一条：全部数据来自离线引擎，**非真实厂商链路**）
- 新增 `docs/11-测试与质量保障.md`：八道门禁 / **两个测试计数口径**（545 个函数 vs 804 个用例，解释差额来自参数化）/ 测试金字塔三层与 24 个文件逐个说明 / ★ **覆盖率如实记录 70%**（含服务层分布表与三行根因分析）/ 隔离矩阵与路由元测试 / 四类契约 / CI 五个 job 与两个刻意取舍 / **`P-01`~`P-08` 修复对照 + 20 条实施中自发现的缺陷** / ★ **测试方法论**（「代理自测全绿 ≠ 功能可用」「会静默通过的检查比没有检查更危险」）
- 新增 `docs/12-项目复盘.md`：度量结果 / 背景与**两个并行目的对决策的影响** / 三个遗留项目的整合决策与**明确不移植的安全缺陷表** / `ADR-01`~`ADR-10` / ★ **两条被实测逼出来的决策**（`ADR-09` 归属落库、`ADR-10` 集合按能力定义，各配「问题 → 根因 → 解法 → 教训」）/ **12 个关键问题**逐个拆解 / 度量结果（含主观自评的「做得好」与「做得勉强」）/ 不足与路线图 / ★ **「如果重做一次会先做什么」5 条** / **8 条可带走的经验沉淀**（每条标注代价）
- 补充 `docs/14-ESP32-S3刷机与联调步骤清单.md` 的「已知局限与取证边界」附录（原缺，由新增的文档门禁报出后补齐）

**P11-B 学习手册（`learning/`，★ 不上传 GitHub）**

- 新增 **20 个文件 / 8177 行**的初级产品经理学习手册（`.gitignore` 已排除，`git ls-files` 中 0 条）：
  `README.md`（12 段结构说明 + 三条阅读路径 + 12 份产物清单）、`00-学习地图.md`（模块依赖图 +
  13 模块时长表 + **三个里程碑** + 四条「不能跳的依赖」+ 卡住时的降级办法）、
  模块 `01`–`12`、练习 `exercises/01`–`06`
- 每个模块**严格 12 段固定结构**：学习目标 / 核心概念（**含生活类比**）/ 本项目中的体现
  （**必须引用真实文件名、表名、端点**）/ 分步讲解 / 重点标注 / 动手练习 / 自测题（**附参考答案**）/
  常见错误（**至少 3 条配本项目真实案例**）/ 与前端沟通要点（**可直接照说的话术**）/
  与后端沟通要点 / 避免关键错误的检查清单 / 拓展知识点
- 6 个练习各含「情境（一封真实口吻的需求消息）/ 输入材料 / 任务分解 / **评分要点** /
  **「如果你的答案被开发这样问，说明哪里没想清」** / 参考思路 / 自查清单 / 进阶挑战」
- ★ **由 4 个独立AI Agent 并行撰写**：每个 prompt 冻结 12 段规范、风格约定、禁止事项与已核对数字，
  并要求「发现 `docs/` 与代码不符时以代码为准并明确指出」
- ★ **本阶段发现并修正 6 处 `docs/` 与代码不一致**（4 处由代理按代码核对发现、2 处由我核对计数发现）：
  ① `docs/08` 能力矩阵把 `mock` 的 `device` 写成「不支持」，实际是 `ALL_CAPABILITIES`（含 `device`）；
  ② `docs/07`/`docs/11` 各有一处权限码拼写错误（`factory:batch_read` 漏一段冒号），
  并因此**给 `check_docs.py` 增加「格式写错」子检查**；③ 门禁类数四处不一致（六件事 / 六类 / 七类）→ 统一为**八类**；
  ④「18 个菜单项（平台 7 / 商户 5 / 工厂 1）」算术不自洽 → 修正为 **13**；
  ⑤「27 条术语表」实际 **24 条**；⑥ `docs/05` 列 19 条索引而 ORM 有 20 处声明 → 写清两个口径，
  并**新增「外键是否真的生效」一节**（`session.py` 逐连接 `PRAGMA foreign_keys=ON`，而 SQLite 默认不强制）
- ★ **客观校验 8 类全部通过**：20 文件齐全 / 12 段结构与顺序 / 自测题含参考答案 /
  **装饰性 emoji 0 处** / **第一人称「我」0 处**（排除引号内话术与反引号内代码）/
  篇幅 445–550 行 / 真实代码标识引用达标 / **198 处相对链接全部可解析**。
  校验脚本**刻意不入库**（`learning/` 不上传，提交的校验器在 CI 与他人机器上会因目录不存在而失败）

**P11-C 收尾（截图 / README 终版 / 最终验证）**

- 新增 **14 张各端截图**（`docs/assets/`，2.2MB）：登录页 + 平台端 6（工作台 / 租户管理 / 订单管理 /
  设备库存 / 工厂订单 / OTA 管理）+ 商户端 4（工作台 / 我的产品 / AI 配置 / 运营看板）+
  工厂端 2（工作台 / 生产订单）+ 小程序 1（扫码页，手机视口）。**逐张人工核验为真实登录态**
- `README.md` 终版：新增「**界面预览**」（7 张内嵌 + 14 张完整清单）、规模速查表、
  「**本项目的验收方式**」（四层手段及各自不可替代的理由）、「**已知局限**」表（合规 / AI 联调 /
  CI / 数据库 / 部署 / 性能 / 测试 / 功能缺口八类）；徽章含 `coverage-70%`（**如实标注**）
- ★ **修复 README 快速开始的真实缺陷**：启动期安全校验要求 **6 项**，而 README 原文只让改
  3 个管理员口令——在干净目录里照字面操作 `make setup` **直接失败**。已列出全部 6 项
  （2 个密钥 + 4 个口令，含 `PLATFORM_OPERATOR_PASSWORD`），并附一段**可选的一行脚本**自动替换
- ★ **两项最终验证通过**：① `learning/` 未被 git 跟踪（`check-ignore` 命中 `.gitignore:15`、
  `git ls-files` 0 条）；② 陌生环境按 README 操作**从克隆到四端全部可访问 ≈ 91 秒**
  （`make setup` 72s + 启动探测 18s，远低于 5 分钟的验收要求）
- ★ **截图采集方式（诚实记录）**：`bsk screenshot` 在本机只能成功一次，之后持续
  `tool RPC timed out after 30s`（同期 `bsk evaluate` 正常）。改用**独立 headless Chrome + 同源引导页**：
  HTTP 登录拿令牌 → 引导页写入 localStorage → 每端一个 `--user-data-dir` → 截目标页 →
  收尾删除引导页并确认工作区干净。截图是**真实服务渲染的真实页面**，只是采集通道换了

### 计划中

- ✅ **P11-C 已处理**（2026-09-17）：① README 快速开始补全 6 项密钥（照字面操作曾直接失败）；② `CHANGELOG` 已切 `[1.0.0]` + 本地 tag；③ `docs/12` 的过期表述已同步；④ README 顶部加了公开定位提示；⑤ CI 加了覆盖率下限 65 并修掉 e2e 重复跑；⑥ **PostgreSQL 已真跑验证**（冒烟 57/57）并因此修掉一个 PG 专有缺陷
- **P11-C 待办（剩余的诚实局限，4 条）**：① **截图由独立 headless Chrome 采集**（`bsk screenshot` 在本机只能成功一次，之后持续 RPC 超时）——是真实服务渲染的真实页面，但采集通道不是用户日常浏览器；② **截图是静态画面**，不含弹窗 / 下拉 / Toast 等交互过程，也不含响应式适配；③ **陌生环境验证的 72 秒偏乐观**（本机 pip 有缓存，冷机器会更久），且**未在真正的另一台机器上验证**（同机不同目录）；④ **`CHANGELOG.md` 未切版本号**（全部变更仍在 `[未发布]` 段，仓库无 git tag）；⑤ **`docs/12` 的「M5 未达成」已过期**（该文档写于 P11-A 阶段，现 M5 已达成，需同步）；⑥ **`docs/` 里仍有个别表述按新口径需复查**（如协作用语统一为「AI Agent」后的一致性）
- **P11-B 待办（学习手册侧的诚实局限，7 条）**：① **`learning/` 不上传 GitHub**，因此本阶段**没有可提交的产物**；② **校验只覆盖结构、覆盖不了内容**（讲得对不对、类比是否恰当、难度是否适合初级 PM 只能人工判断）；③ **未做真人试读**，所有难度判断都是作者自我估计；④ **篇幅硬上限导致压缩**（模块 03 的 RICE/MoSCoW/Kano、模块 04 的主键外键与实体属性、模块 12 的自检评分表都被合并或一句话带过）；⑤ 无截图与配图（全用 mermaid 文本图）；⑥ 模块 04 的索引条数有两个口径（19 条表格 vs 20 处 ORM 声明）；⑦ **结构校验器不入库、无单元测试、不在 CI 中**
- **P11-A 待办（文档侧的诚实局限，7 条）**：① **文档集不含截图**（`docs/assets/` 为空）；② `docs/04` PRD 是**事后补写**，无用户调研与需求优先级；③ `docs/10` 的基准数据来自**离线模拟引擎**，不代表真实厂商链路；④ **门禁只保证「名字与数字对得上」**，叙述性描述无法校验；⑤ `docs/05` 的字段类型未逐列核对 SQLite 实际 DDL；⑥ `scripts/check_docs.py` **自身没有单元测试**；⑦ `docs/03`/`docs/12` 的分层与决策解释属事后归纳
- **P10 待办（部署侧的诚实局限，7 条）**：① **CI 从未在真实 GitHub 上跑过**（本机无 remote）——`ci.yml` 的命令与 `make check` 逐字一致，但 YAML 语法与表达式只做了本地静态审查，首次推送后必须实跑确认；② **CI 不构建镜像**（pip 全量安装是分钟级，理由写在 `ci.yml` 注释里）；③ **`--profile postgres` 只验证了配置可解析**，未真跑 PG 实例，到 asyncpg 的连接串与实际迁移未在 PG 上执行过；④ **Nginx 只验证了 HTTP 与 WebSocket 转发**，TLS（配置里预留 80→443 注释段）与多实例负载均衡（`upstream` 只有一个 server）未验证；⑤ **`scripts/reset_db.sh` 是同盘备份**，宿主机磁盘故障时备份与数据一起丢；⑥ **SQLite 写并发能力有限**，容器内 4 个 uvicorn worker 跑演示没问题，真实多租户并发写应切 PostgreSQL；⑦ **冒烟脚本只覆盖读路径 +2 个幂等写**，写路径验证依赖 `make test`（隔离测试库）
- **P9 待办**：运营快照的**时区口径**（现按 UTC 归档，与商户本地日期可能差一天；`tenants.timezone` 已存在但未参与聚合）；内容库的写能力与内容审核流程；知识库向量化检索（现为关键词级，无 embedding 服务）；知识库注入对话的上限（5 文件 / 20 块）；指标表行主键改用 `ids.py` 登记的前缀
- **P6 未交付项**：工厂端「批次查询」页（菜单项显示「建设中」；`factory:batch:read` 权限与 `/platform/batches` 端点已存在，缺 `/factory/batches` 端点与只读页）
- **待收紧项**：`factory_orders.order_id` 无唯一索引，「一单一张工单」目前是**服务层**约束，并发下理论上可派两次
- **P8 待办**：`frontend/shared/core/ws.js` 与真实协议四处不符（URL / 令牌来源 / 帧名 / SSE 用 GET），当前被小程序端「继承覆盖」绕开，共享模块本身不可用；`frontend/shared/core/api.js` 无条件注入后台令牌，终端用户端只能另起一层；语音（`user.audio` / `assistant.audio`）前端未接

### 说明

**迁移编号调整**：P1 落地时实际占用了 `0001`–`0004`（与原始计划的 0001/0002 不同），因此目录域顺延为 `0005`/`0006`，P7 使用 `0007`；P4 起顺延为 `0008`–`0010`（订单 / 设备 / 批次），P5 为 `0011`，P6 为 `0012`（工厂账号归属）+ `0013`（设备工单归属），**P8 为 `0014`（终端用户与充值，原属 P9），P9 为 `0015`（运营指标 / 内容库 / OTA）**；**P10 无迁移**（纯部署与质量保障）。

**测试规模**：后端 118 → **809** 条（P3 82；P7 49；P4 146；P5 53；P6 281；P8 20 + 全闭环 e2e 1；**P9 46**：租户隔离 26 + 指标 9 + OTA 14）。**P10 未新增单元测试**，新增的是部署期验证：全端点冒烟 57 项、持久化一致性检查、敏感信息扫描 236 文件 0 命中、经 Nginx 的 WebSocket 握手。**P11 新增 5 个用例**（`test_sql_dialect_portability.py`，3 个函数）——这是 P11 唯一的单元测试，用来守卫「日期必须绑 `date` 而不是 `str`」这条**在 SQLite 上复现不出来**的 PG 专有约束（详见 `## 7.5`）。其余 P11 产出是文档一致性门禁（八类校验）、文档集（14 篇）、学习手册（20 文件 / 8177 行，不入库）、14 张各端截图与 README 终版——**文档与手册阶段靠「一致性门禁 + 换个视角复核」而不是靠测试**。

**两个计数口径**：**548 个测试函数**（`def test_` 计数，25 个文件）/ **809 个用例**（pytest 实际执行数）。差额来自 `@pytest.mark.parametrize` 展开（例：租户隔离矩阵把「端点 × 越权形态」展开）。

**覆盖率**：**70%**（10354 语句 / 3108 未覆盖）。`ai_config_service` 29% / `metrics_service` 29% / `ota_service` 30% 是三个最低点——★ **「804 passed」不等于「测试充分」**，详情与根因见 `docs/11`。

**验收强度（本项目的固定做法）**：每个阶段除自动化测试外，都要在**真实服务**上跑一遍端到端接口验收，并用 `browser-skill` 驱动真实 Chromium 点一遍关键路径。累计：P8 接口验收 50/50 + 浏览器 11 项；P9 浏览器 6 项；**P10 冒烟 57/57（直连与经 Nginx 各一次）+ 容器 healthy + 起停数据一致**；**P11-A `make check` 全绿（804 passed in 310.55s / mypy 95 文件 / 契约一致 / 文档一致性通过）**；**P11-C 陌生环境按 README 从克隆到四端可用 ≈91 秒**。

**文档阶段的一条对应做法**：文档不能被「读一遍觉得对」验收，因此新增了 `make docs-check`。
两个手段最有效：① **用脚本生成而不是手抄**——`docs/05` 的 655 个字段由 ORM 元数据导出、
`docs/06` 的 145 个端点由源码 AST 与快照联合生成**并在生成时断言两侧集合相等**；
② **把脱节变成红灯**——八类校验接入 `make check` 与 CI。
首次运行即报出 5 处问题（含 1 处会让合计对不上的手写数字错误），证明它不是形式主义。

**这条做法在 P10 上第三次证明了自己**：P8 的真缺陷是浏览器点出来的（自动化测试覆盖的是「我以为的路径」），而 P10 这 12 个问题**没有一个是读代码或跑集成测试能发现的**——`make up` 从 P0 起就是坏的、nginx 的 WS 路由写错、未指纹化的 JS 被声明强缓存一年、容器的 `FRONTEND_ROOT` 推算到了 `/` 导致整个 UI 404 而 API 正常……**可部署性只能靠真的部署一次来证明**。同一逻辑还有一条推论：`smoke_test.sh` 曾在**误删计数函数后仍打印「全部通过」**——**会静默通过的检查比没有检查更危险**，因此给脚本加了最低断言数守卫。

---

## [0.1.0] - 2026-09-16

### 新增

**项目脚手架**

- 完整目录结构（后端 / 前端 / 部署 / 脚本 / 文档 / 测试）
- `.gitignore`，排除 `.env`、`learning/`、`data/`、`__pycache__/`、`*.jar`
- `.env.example`，涵盖应用、数据库、鉴权、AI 厂商、对象存储、可观测性共十一大类配置，全部含中文注释且无真实密钥
- `Makefile`，提供 30+ 个统一开发命令（`help` / `setup` / `dev` / `test` / `migrate` / `seed` / `up` / `down` 等）
- `LICENSE`（MIT）
- `README.md`，含项目简介、四端角色、快速开始、架构图、技术栈、文档索引
- `CONTRIBUTING.md`，含代码规范、提交规范、测试要求与**八条架构红线**
- `SECURITY.md`，含安全设计说明、生产部署安全清单与已知安全边界
- `docker-compose.yml`，支持 SQLite 零依赖模式与 PostgreSQL 可选模式
- `deploy/Dockerfile.backend`，多阶段构建、非 root 运行、内置健康检查
- `deploy/nginx.conf`，含 API 反代、WebSocket 升级、流式响应免缓冲、静态资源缓存策略
- `backend/requirements.txt` 与 `requirements-dev.txt`
- `backend/pyproject.toml`，配置 Ruff、mypy（strict）、pytest、coverage

**项目管理**

- `todolist.md`：P0–P11 全量任务清单，含遗留缺陷修复对照表、安全缺陷处理表、架构决策记录（ADR）
- `项目进度.md`：进度追踪表，含总体进度条、里程碑、阻塞登记与更新约定

### 说明

- 本项目由三个遗留项目整合而成，整合策略为「各取所长」：
  - 静态原型提供 UI/交互规范、领域模型与 PRD
  - 参考实现的运行时快照提供数据库 Schema 结构与多租户 RBAC 设计
  - 可运行 MVP 提供业务闭环语义（幂等绑定、confirm-token、脱敏、审计）
- **安全变更**：彻底移除历史版本中的硬编码弱口令，改为必须通过环境变量注入强密码，未配置时服务拒绝启动
- **安全变更**：厂商适配器在未配置密钥时返回 `VENDOR_UNAVAILABLE`，不再返回伪造的成功结果
- **安全变更**：修正了历史实现中二维码生成函数的参数顺序缺陷（租户标识曾被错误传入 `tenant_id` 位置）

---

[未发布]: ../../compare/v1.0.0...HEAD
[1.0.0]: ../../releases/tag/v1.0.0
[0.1.0]: ../../releases/tag/v0.1.0
