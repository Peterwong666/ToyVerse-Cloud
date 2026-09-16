"""单元测试：工厂端脱敏纯函数 + 工单字段白名单（P6 红线，无 IO）。

本文件独立验证 P6 的两处「纯逻辑」安全收口，它们都不依赖数据库与 HTTP：

1. :func:`app.services.serializers.mask_customer_name` —— 客户名的唯一表达方式；
2. :class:`app.services.serializers.FactoryOrderSerializer` 的字段白名单
   与响应模型字段集合的一致性；
3. :func:`app.services.serializers.burn_progress_percent` —— 工单进度口径。

为什么边界表和不变量都要写
--------------------------
只列举「中国移动 → 中**动」这类取值是不够的：它只能证明**这几个**输入正确，
而脱敏函数真正的风险是某个长度分支写错（例如 ``len - 2`` 在 len=2 时为 0，
等于没脱敏）。因此这里对每个长度都单独钉一条，并把「长度关系 / 首尾保留 /
中间全星号 / 幂等 / 确定性」这些**不变量**单独成组——不变量才能覆盖
「没被列举到的输入」。

每条断言都对应一条业务规则：
* 首尾保留 → 工厂能靠脱敏名做对账（同一客户的多张工单脱敏成同一串）；
* 中间全遮 → 无法从响应还原客户名（脱敏的全部意义）；
* 幂等 / 确定性 → 工厂不会因为「同一客户两次显示不同」而误判客户换了人；
* 白名单一致 → 新增敏感字段时必须同时改三处，否则测试失败（防静默泄漏）。
"""

from __future__ import annotations

import re

import pytest

from app.db.base import utcnow
from app.models.enums import (
    FACTORY_ORDER_STATUS_LABELS,
    FACTORY_ORDER_TERMINAL_STATUSES,
    FACTORY_ORDER_TRANSITIONS,
    INSPECTION_RESULT_LABELS,
    FactoryOrderStatus,
    InspectionResult,
)
from app.models.factory import FactoryOrder
from app.schemas.factory import FactoryOrderDetailResponse, FactoryOrderResponse
from app.services.serializers import (
    MASK_CHAR,
    FactoryOrderSerializer,
    burn_progress_percent,
    mask_customer_name,
)

pytestmark = pytest.mark.unit


#: 正常客户名（用于「必须真的被遮住」的不变量）。
#:
#: 刻意包含中英文、括号、空格、多字符与异形符号：脱敏按**字符**计数，
#: 全角字符与空格的宽度与 ASCII 不同，若实现里用了 ``len(bytes)`` 之类的
#: 错误度量，这一组会立刻暴露。
NORMAL_NAMES: tuple[str, ...] = (
    "中国移动",
    "Acme",
    "Acme Corporation",
    "星辰玩具科技有限公司",
    "中国电信集团",
    "深圳体验店",
    "Acme (中国) Ltd",
    "!@#",
    "A B C",
)

#: 退化输入：整串恰好由脱敏字符 ``*`` 组成（或中段恰好是 ``*``）。
#:
#: **不是**真实客户名，单独成组记录实际行为——见
#: :meth:`TestMaskSecurityInvariants.test_degenerate_mask_char_input_is_a_fixed_point`
#: 与报告中的「语义可疑但不算缺陷」条目。
DEGENERATE_NAMES: tuple[str, ...] = ("中*动", "***", "**", "*")

#: 退化输入中**真正**构成不动点的那部分（长度 ≥ 2）：
#: 它们满足 ``mask(x) == x``。长度 1 的 ``"*"`` 会变成 ``"**"``（长度 +1），
#: 与「2 字特例」同因——不是不动点，故不列入。
DEGENERATE_FIXED_POINTS: tuple[str, ...] = ("中*动", "***", "**")


# ===========================================================================
# 一、边界全表：按「长度分支」逐条钉死
# ===========================================================================


class TestMaskBoundaryTable:
    """逐长度的取值边界。每条都对应脱敏规则里的一个分支。"""

    def test_length_one_keeps_single_star(self) -> None:
        """1 字输入：没有任何中间字符可遮，只能补一个星号。

        若这里返回原文（长度 1 时 ``len - 2`` 为负会切出空串），
        等于单字客户名完全不脱敏——而单字中文名（如「李」）在通讯录里很常见。
        """
        assert mask_customer_name("中") == "中*"
        assert mask_customer_name("A") == "A*"

    def test_length_two_keeps_single_star(self) -> None:
        """2 字输入：``len - 2 == 0`` 会让星号数为 0，必须走特例分支。

        这是最容易写错的一个长度：若实现只有 ``首 + '*'*(len-2) + 尾``
        这一条规则，2 字输入会**原样返回**（等于没脱敏），
        而且因为「看起来长度没变」很难在人工验收里发现。
        """
        assert mask_customer_name("中国") == "中*"
        assert mask_customer_name("AB") == "A*"

    def test_length_three(self) -> None:
        """3 字输入：刚好遮住中间 1 个字符。"""
        assert mask_customer_name("中国移") == "中*移"

    def test_length_four_chinese_matches_documented_example(self) -> None:
        """4 字中文：``中国移动`` → ``中**动``（用户明确要求的示例）。"""
        assert mask_customer_name("中国移动") == "中**动"
        assert mask_customer_name("中国移动") == "中" + "*" * 2 + "动"

    def test_long_chinese_name(self) -> None:
        """多字中文：星号数 = 长度 - 2，逐字符计算（不是按字节 / 拼音）。"""
        name = "星辰玩具科技有限公司"
        expected = "星" + "*" * (len(name) - 2) + "司"
        assert mask_customer_name(name) == expected
        assert len(mask_customer_name(name)) == len(name)

    def test_english_four_letters(self) -> None:
        """英文 4 字母：``Acme`` → ``A**e``（与中文同一规则，不加特例）。"""
        assert mask_customer_name("Acme") == "A**e"

    def test_english_two_letters(self) -> None:
        """英文 2 字母：``AB`` → ``A*``（与中文 2 字同一条特例）。"""
        assert mask_customer_name("AB") == "A*"
        assert mask_customer_name("AB") != "AB"

    def test_english_with_spaces_and_multiple_words(self) -> None:
        """含空格的英文名：空格按字符计数，只保留首尾字符。"""
        name = "Acme Corporation"
        masked = mask_customer_name(name)
        assert masked == "A" + "*" * (len(name) - 2) + "n"
        # 词边界不能泄漏（中间不能出现空格——否则能数出客户名有几个词）
        assert " " not in masked

    def test_brackets_and_spaces_in_middle_are_masked(self) -> None:
        """含括号的客户名：括号等标点同样是「中间字符」，必须全部遮掉。"""
        name = "Acme (中国) Ltd"
        masked = mask_customer_name(name)
        assert masked[0] == "A"
        assert masked[-1] == "d"
        assert set(masked[1:-1]) == {MASK_CHAR}
        for leaked in ("(", ")", "中", "国", "c", "m", "e"):
            assert leaked not in masked, f"中间字符「{leaked}」不应出现在脱敏结果里"

    def test_pure_symbols(self) -> None:
        """纯符号：脱敏与普通文本同规则（不因为「不是名字」就跳过）。"""
        assert mask_customer_name("!@#") == "!*#"
        assert mask_customer_name("!!!") == "!*!"

    def test_surrounding_whitespace_is_counted_as_characters(self) -> None:
        """前后空白**不被** strip，按字符参与脱敏。

        记录实际行为：``"  中国移动  "``（长 8）→ 首字符是空格、尾字符是空格，
        中间 6 个星号。这不构成泄漏（中间内容仍全部遮住），
        但对账时要注意「同一位客户的名字在不同入口可能带不同空白，
        脱敏后看起来不一样」——因此它记录的是边界而不是缺陷。
        """
        masked = mask_customer_name("  中国移动  ")
        assert len(masked) == 8
        assert masked[0] == " "
        assert masked[-1] == " "
        assert set(masked[1:-1]) == {MASK_CHAR}

    def test_empty_string_returns_empty(self) -> None:
        """空串：返回空串而不是 ``"*"``。

        若返回 ``"*"``，前端会把「没有客户名」渲染成一个看起来像脱敏值的占位，
        让人误以为有客户信息被遮住了。
        """
        assert mask_customer_name("") == ""

    def test_none_returns_empty(self) -> None:
        """``None``（关联行缺失）与空串等价，不能让整页 500。"""
        assert mask_customer_name(None) == ""

    def test_non_empty_input_never_returns_empty(self) -> None:
        """反向确认：非空输入绝不能返回空串（空串会被前端当成「无数据」）。"""
        for name in NORMAL_NAMES:
            assert mask_customer_name(name) != ""


# ===========================================================================
# 二、不变量（比列举取值更重要）
# ===========================================================================


class TestMaskInvariants:
    """脱敏的通用性质：对**任意**输入都成立，而不只是被列举的那几个。"""

    @pytest.mark.parametrize("name", NORMAL_NAMES + DEGENERATE_NAMES)
    def test_length_relation(self, name: str) -> None:
        """长度关系：``len >= 3`` 时长度不变；``len <= 2`` 时恒为 2。

        长度不变是「可对账」的前提：工厂端的表格列宽与导出格式都依赖它；
        而 ``len <= 2`` 时为 2 是刻意的（见边界表）——长度会**变长**，
        这是「必须遮住至少一个字符」与「不泄漏长度信息」之间的取舍，
        这里把它钉成契约而不是留给实现自由发挥。
        """
        masked = mask_customer_name(name)
        expected_len = len(name) if len(name) >= 3 else 2
        assert len(masked) == expected_len, f"{name!r} 脱敏后长度不符：{masked!r}"

    @pytest.mark.parametrize("name", NORMAL_NAMES)
    def test_first_and_last_characters_are_preserved(self, name: str) -> None:
        """首尾字符保留：工厂靠它确认「这单还是那个客户」。

        如果首尾也被遮掉，工厂就无法区分「同一客户的第 3 单」与「新客户」，
        对账只能靠工单号——这正是保留首尾的原因。
        """
        masked = mask_customer_name(name)
        assert masked[0] == name[0]
        assert masked[-1] == name[-1]

    @pytest.mark.parametrize("name", NORMAL_NAMES)
    def test_middle_is_entirely_mask_char(self, name: str) -> None:
        """中间部分全部是星号：没有「部分遮挡」（如只遮一半）的实现空间。

        部分遮挡会让攻击者用多个已知客户名做差分，把未遮蔽的片段拼回来。
        """
        masked = mask_customer_name(name)
        assert set(masked[1:-1]) == {MASK_CHAR}, f"{name!r} → {masked!r} 中间含非星号字符"

    @pytest.mark.parametrize("name", NORMAL_NAMES + DEGENERATE_NAMES)
    def test_idempotent(self, name: str) -> None:
        """幂等：对已脱敏结果再脱敏必须得到**同一个**字符串（不会变短到 0）。

        判断依据（为什么它必然成立）：脱敏结果要么长度与原输入相同（``len >= 3``），
        要么长度为 2（``len <= 2``）；两种情况的首尾字符都与输入一致、
        中间全是星号。再次脱敏只依赖「长度 + 首尾字符」，因此结果逐字节相同。
        业务含义：工厂端把脱敏名再喂给任何「二次脱敏」的展示层时，
        不会出现「同一个客户在列表页与详情页显示成两个名字」。
        """
        once = mask_customer_name(name)
        twice = mask_customer_name(once)
        assert twice == once, f"{name!r}：一次 {once!r}，二次 {twice!r}"
        assert len(twice) >= 2, "二次脱敏不能退化成空串"

    @pytest.mark.parametrize("name", NORMAL_NAMES + DEGENERATE_NAMES)
    def test_deterministic(self, name: str) -> None:
        """确定性：同一输入必得同一输出。

        工厂端用脱敏名做对账（「这两张工单是同一个客户下的」），
        一旦实现引入随机盐 / 时间戳，对账就失去依据。
        """
        assert mask_customer_name(name) == mask_customer_name(name)

    @pytest.mark.parametrize("name", NORMAL_NAMES)
    def test_masked_result_is_several_calls_stable(self, name: str) -> None:
        """跨多次调用稳定（排除「首次调用初始化随机状态」这类实现）。"""
        results = {mask_customer_name(name) for _ in range(5)}
        assert len(results) == 1, f"同一输入产生多个结果：{results}"


class TestMaskSecurityInvariants:
    """安全侧不变量：遮住的内容必须真的遮住。"""

    @pytest.mark.parametrize("name", NORMAL_NAMES)
    def test_result_never_equals_original(self, name: str) -> None:
        """★ 长度 ≥ 3 的输入，脱敏结果绝不能等于原文。

        为什么单独成条：如果实现写成 ``首 + name[1:-1] + 尾``（漏了替换），
        长度检查、首尾检查、幂等检查**全都会通过**——只有这一条能发现它。
        """
        if len(name) < 3:
            pytest.skip("长度 < 3 的输入由边界表覆盖（结果长度会变）")
        assert mask_customer_name(name) != name

    @pytest.mark.parametrize("name", NORMAL_NAMES)
    def test_middle_characters_do_not_appear_in_result(self, name: str) -> None:
        """★ 结果中不得出现原文的**中间**字符。

        业务规则：工厂端不得从 ``customerNameMasked`` 反推客户名。
        只要中间有一个字符漏出来，配合行业知识就足以猜出客户。
        """
        masked = mask_customer_name(name)
        for char in set(name[1:-1]):
            assert char not in masked, f"{name!r} 的中间字符「{char}」出现在 {masked!r} 中"

    def test_mask_char_constant_is_asterisk(self) -> None:
        """脱敏字符是 ``*``：前端与文档都按它渲染，换成别的字符会无声破版。"""
        assert MASK_CHAR == "*"
        assert MASK_CHAR in mask_customer_name("中国移动")

    def test_output_shape_matches_regex(self) -> None:
        """形状契约：``首字符 + 若干星号 + 尾字符``（长度 ≥ 3）。"""
        pattern = re.compile(r"^.\*+.$")
        for name in NORMAL_NAMES:
            assert pattern.match(mask_customer_name(name)), name

    def test_degenerate_mask_char_input_is_a_fixed_point(self) -> None:
        """⚠ 记录一个**语义可疑但不算缺陷**的边界：整串由 ``*`` 构成的输入是脱敏不动点。

        ``mask("中*动") == "中*动"``、``mask("***") == "***"``——
        「结果 != 原文」这条不变量在**这种输入上不成立**。

        为什么判定为「不算缺陷」：客户名来源于 ``tenants.name``（平台运营录入），
        不会有人把客户名填成 ``中*动``；而函数本身是「按位置遮挡」，
        位置上的字符恰好是星号时它无从区分。真正需要防的是
        「真实客户名被原样下发」，这一条由上面针对 NORMAL_NAMES 的断言覆盖。
        本用例把该边界**钉成已知行为**，避免后续有人误以为发现了漏洞。
        """
        for name in DEGENERATE_FIXED_POINTS:
            assert mask_customer_name(name) == name, f"{name!r} 应为不动点"
        # 长度 1 的 ``"*"`` 不是不动点：走「2 字特例」变成 ``"**"``（长度 +1）。
        # 这是长度 1 的通用行为（``"中"`` → ``"中*"``），不是星号特有的问题。
        assert mask_customer_name("*") == "**"


# ===========================================================================
# 三、字段白名单与响应模型一致性
# ===========================================================================


class TestFactoryOrderWhitelist:
    """白名单是「允许下发的字段」的权威清单，必须与响应模型逐字对齐。"""

    def test_allowed_fields_match_response_model_exactly(self) -> None:
        """``ALLOWED_FIELDS`` 与 ``FactoryOrderResponse.model_fields`` 完全一致。

        为什么要求「完全一致」而不是「包含」：白名单是给测试与评审看的
        可执行契约；只要两者出现差额，要么是响应多下发了一个没人审过的字段
        （泄漏风险），要么是白名单里留了一个根本不存在的字段（契约失真）。
        新增字段必须同时改「模型 / 序列化器 / 白名单」三处，本用例就是那道闸门。
        """
        model_fields = set(FactoryOrderResponse.model_fields.keys())
        assert model_fields == set(FactoryOrderSerializer.ALLOWED_FIELDS), (
            f"模型独有 {model_fields - set(FactoryOrderSerializer.ALLOWED_FIELDS)}；"
            f"白名单独有 {set(FactoryOrderSerializer.ALLOWED_FIELDS) - model_fields}"
        )

    def test_detail_model_is_a_strict_superset_of_allowed_fields(self) -> None:
        """详情模型的字段必须**包含**白名单全部字段（详情在列表项基础上扩展）。"""
        detail_fields = set(FactoryOrderDetailResponse.model_fields.keys())
        assert set(FactoryOrderSerializer.ALLOWED_FIELDS) <= detail_fields

    def test_denied_and_allowed_are_disjoint(self) -> None:
        """允许集与禁止集不能有交集（否则「禁止」形同虚设）。"""
        overlap = set(FactoryOrderSerializer.ALLOWED_FIELDS) & set(
            FactoryOrderSerializer.DENIED_FIELDS
        )
        assert overlap == frozenset(), f"白名单与禁发名单冲突：{overlap}"

    def test_denied_fields_absent_from_both_response_models(self) -> None:
        """★ 禁发字段一个都不能出现在列表项与详情模型里。

        这是第二层保护的**类型级**证据：金额 / 申请人联系方式 / 真实客户名
        在工厂端响应模型里无法表达，因此「某处忘了删」这种错误写不出来。
        """
        list_fields = set(FactoryOrderResponse.model_fields.keys())
        detail_fields = set(FactoryOrderDetailResponse.model_fields.keys())
        for denied in FactoryOrderSerializer.DENIED_FIELDS:
            assert denied not in list_fields, f"{denied} 不得出现在列表项模型里"
            assert denied not in detail_fields, f"{denied} 不得出现在详情模型里"

    def test_allowed_fields_contain_no_amount_or_contact_names(self) -> None:
        """白名单里不得出现金额 / 联系方式 / 邮箱语义的字段名。

        防止有人「顺手」把 ``unit_price`` 加进白名单——那是一条
        比测试失败更隐蔽的路：白名单与模型会同时被改，一致性断言仍然通过。
        因此这里对**字段名语义**单独设一条。
        """
        forbidden_substrings = ("price", "amount", "phone", "mobile", "email", "contact")
        for field in FactoryOrderSerializer.ALLOWED_FIELDS:
            lowered = field.lower()
            for token in forbidden_substrings:
                assert token not in lowered, f"白名单字段 {field} 含敏感语义「{token}」"

    def test_customer_name_has_exactly_one_masked_expression(self) -> None:
        """客户名在工厂端只有一个表达：``customer_name_masked``。

        只要它存在，就不会有人再补一个未脱敏的 ``customer_name``。
        """
        fields = set(FactoryOrderResponse.model_fields.keys())
        assert "customer_name_masked" in fields
        assert "customer_name" not in fields


# ===========================================================================
# 四、序列化器实际输出
# ===========================================================================


def _bare_factory_order(**overrides: object) -> FactoryOrder:
    """构造一个**不落库**的工单 ORM 对象（纯函数测试不连数据库）。

    ``TimestampMixin`` 的 ``created_at`` 只在 flush 时由默认值填充，
    因此这里显式传入——否则响应模型会拿到 ``None`` 而校验失败。
    """
    base: dict[str, object] = {
        "id": "fo-unit-001",
        "factory_order_no": "FO-20260101-ABCD",
        "order_id": "o-unit-001",
        "factory_id": "f-unit-01",
        "product_model": "ESP32-S3",
        "firmware_version": "1.0.0",
        "quantity": 10,
        "burned_count": 4,
        "status": str(FactoryOrderStatus.PRODUCING),
        "assigned_at": utcnow(),
        "due_at": None,
        "shipped_at": None,
        "remark": "产线备注",
        "created_at": utcnow(),
    }
    base.update(overrides)
    return FactoryOrder(**base)  # type: ignore[arg-type]


class TestSerializerOutput:
    """序列化器的**实际输出**必须正好落在白名单内。"""

    def test_to_response_emits_exactly_allowed_fields(self) -> None:
        """★ 序列化输出的键集合 == ``ALLOWED_FIELDS``。

        前一条断言的是「模型与白名单一致」，这一条断言的是「序列化器**真的**
        只产出这些字段」。两者缺一不可：模型对了但序列化器多传一个字段，
        会直接被 Pydantic 拒绝（这是好事）；但如果序列化器将来改成
        ``model_validate(order)``，多出来的 ORM 字段就会**静默**通过——
        本用例钉住的正是这个改动方向。
        """
        response = FactoryOrderSerializer().to_response(
            _bare_factory_order(),
            factory_name="星辰智造",
            order_no="ORD-20260101-ABCD",
            customer_name="中国移动通信集团",
        )
        assert set(response.model_dump().keys()) == set(FactoryOrderSerializer.ALLOWED_FIELDS)

    def test_to_response_masks_customer_name_and_keeps_progress(self) -> None:
        """列表项：客户名已脱敏，进度按 ``burned / quantity`` 计算。"""
        response = FactoryOrderSerializer().to_response(
            _bare_factory_order(quantity=10, burned_count=4),
            factory_name="星辰智造",
            order_no="ORD-20260101-ABCD",
            customer_name="中国移动",
        )
        assert response.customer_name_masked == "中**动"
        assert response.progress_percent == 40.0
        assert response.remaining == 6
        assert response.status_label == "生产中"

    def test_to_response_none_customer_name_becomes_empty(self) -> None:
        """订单行缺失（客户名拿不到）时脱敏名为空串，而不是 ``None``。"""
        response = FactoryOrderSerializer().to_response(
            _bare_factory_order(),
            factory_name=None,
            order_no=None,
            customer_name=None,
        )
        assert response.customer_name_masked == ""
        assert response.order_no is None

    def test_to_detail_adds_only_non_sensitive_extras(self) -> None:
        """★ 详情的**增量字段**里不得出现禁发字段。

        详情是「列表项 + 备注 + 订单侧状态 + 明细」，增量部分同样要过一遍
        禁发名单——否则「详情忘了脱敏」会从这个缺口重新出现。
        """
        detail = FactoryOrderSerializer().to_detail(
            _bare_factory_order(),
            factory_name="星辰智造",
            order_no="ORD-20260101-ABCD",
            customer_name="中国移动",
        )
        detail_keys = set(detail.model_dump().keys())
        assert set(FactoryOrderSerializer.ALLOWED_FIELDS) <= detail_keys
        extras = detail_keys - set(FactoryOrderSerializer.ALLOWED_FIELDS)
        assert extras == {
            "production_note",
            "order_quantity",
            "order_status",
            "order_status_label",
            "burn_reports",
            "inspections",
            "inspection_summary",
        }, f"详情增量字段发生变化：{extras}"
        assert not (extras & set(FactoryOrderSerializer.DENIED_FIELDS))
        # 备注映射自 ORM 的 remark（平台运营自己填的内容）
        assert detail.production_note == "产线备注"


# ===========================================================================
# 五、烧录进度口径
# ===========================================================================


class TestBurnProgressPercent:
    """``progress_percent`` 的边界与口径。

    业务口径：进度按**累计烧录 / 委托数量**计算（不用工单数平均），
    上限 100（防脏数据把进度条画到 120%），``quantity == 0`` 时返回 0 而不是
    抛 ``ZeroDivisionError``（一张数量为 0 的脏工单不该让整个列表页 500）。
    """

    def test_zero_quantity_returns_zero_not_exception(self) -> None:
        """数量为 0：返回 0.0。

        业务规则：``ZeroDivisionError`` 会让「工单列表」与「工作台统计」
        在遇到一张脏工单时整页 500——用户看不到任何数据，
        而正确行为是让那张单显示 0% 并继续可见。
        """
        assert burn_progress_percent(0, 0) == 0.0
        assert burn_progress_percent(5, 0) == 0.0

    def test_negative_quantity_returns_zero(self) -> None:
        """负数量（脏数据）同样返回 0.0，不抛异常。"""
        assert burn_progress_percent(5, -10) == 0.0

    def test_zero_burned_is_zero_percent(self) -> None:
        """未开始烧录：0%。"""
        assert burn_progress_percent(0, 10) == 0.0

    def test_full_burn_is_exactly_hundred(self) -> None:
        """烧满：恰好 100.0（不能是 99.9，否则前端进度条永远差一点）。"""
        assert burn_progress_percent(10, 10) == 100.0

    def test_over_burn_is_truncated_to_hundred(self) -> None:
        """★ 超量上报（脏数据）：截断到 100，不能出现 >100 的进度。

        业务规则：进度条画到 120% 会让前端样式错位；
        而「超量」本身应当由 ``BURN_COUNT_EXCEEDED`` 在写入侧拦住，
        这里的截断只是展示层的防御。
        """
        assert burn_progress_percent(12, 10) == 100.0
        assert burn_progress_percent(1000, 10) == 100.0

    @pytest.mark.parametrize(
        ("burned", "quantity", "expected"),
        [
            (1, 3, 33.3),
            (2, 3, 66.7),
            (1, 8, 12.5),
            (1, 6, 16.7),
            (3, 8, 37.5),
            (1, 100, 1.0),
            (99, 100, 99.0),
        ],
    )
    def test_normal_values_use_one_decimal(self, burned: int, quantity: int, expected: float) -> None:
        """正常值：四舍五入到**一位小数**。

        一位小数是前后端契约（进度文案如「33.3%」）；
        若实现返回未舍入的 ``33.333333333333336``，前端会出现
        「33.333333333333336%」这种把布局撑破的文本——本用例对每个值
        同时断言取值与「确实只有一位小数」。
        """
        result = burn_progress_percent(burned, quantity)
        assert result == expected
        assert round(result, 1) == result

    def test_returns_float_type(self) -> None:
        """返回类型是 ``float``（前端按数字处理，不是字符串）。"""
        assert isinstance(burn_progress_percent(1, 2), float)

    def test_negative_burned_is_not_floored_at_zero(self) -> None:
        """⚠ 记录一处**语义可疑但不算缺陷**的行为：负的 ``burned_count`` 会得到负进度。

        ``min(burned / quantity, 1.0)`` 只设了**上限**，没有下限，
        因此 ``burn_progress_percent(-5, 10) == -50.0``——进度条会画成负数宽度。

        为什么判定为「不算缺陷」：``burned_count`` 由服务层累加且
        ``BurnReportRequest`` 强制 ``gt=0``，正常路径下不可能为负；
        这条记录的是「展示层对极端脏数据没有下限保护」这一事实。
        若将来手工修库 / 数据迁移引入了负值，前端需要自己兜底。
        """
        assert burn_progress_percent(-5, 10) == -50.0


# ===========================================================================
# 六、工厂状态机与展示名契约（纯枚举，无 IO）
# ===========================================================================


class TestFactoryOrderStateMachine:
    """工单状态机是 P6 三条状态链里唯一**没有历史单测**的一条，这里补上。

    它与设备 / 订单状态机一样，是「业务事实」而不是实现细节：
    ``factory_service._transition_factory_order`` 每次迁移都要查这张表，
    表错一条边就会在生产上出现「状态自己变了」的工单。
    """

    def test_happy_path_is_fully_connected(self) -> None:
        """主干 ``PENDING → PRODUCING → COMPLETED → SHIPPED`` 每一步都合法。"""
        assert FactoryOrderStatus.PRODUCING in FACTORY_ORDER_TRANSITIONS[FactoryOrderStatus.PENDING]
        assert (
            FactoryOrderStatus.COMPLETED in FACTORY_ORDER_TRANSITIONS[FactoryOrderStatus.PRODUCING]
        )
        assert FactoryOrderStatus.SHIPPED in FACTORY_ORDER_TRANSITIONS[FactoryOrderStatus.COMPLETED]

    def test_no_shortcut_from_pending_to_completed(self) -> None:
        """★ ``PENDING`` 不能直达 ``COMPLETED``。

        业务规则：工单必然经历过「生产中」。若存在捷径，
        「一次报满」的工单就不会留下 ``PRODUCING`` 这一段，
        状态分布统计与「产线开工到完工的时长」这类口径随之失效。
        （实现因此在报满时先补一次 ``PENDING → PRODUCING``——
        本用例钉住的正是它为什么要补那一步。）
        """
        assert (
            FactoryOrderStatus.COMPLETED
            not in FACTORY_ORDER_TRANSITIONS[FactoryOrderStatus.PENDING]
        )
        assert FactoryOrderStatus.SHIPPED not in FACTORY_ORDER_TRANSITIONS[FactoryOrderStatus.PENDING]

    def test_shipped_is_terminal(self) -> None:
        """``SHIPPED`` 是终态：没有任何出边（出货后再报烧录 / 再出货都不可达）。"""
        assert FACTORY_ORDER_TRANSITIONS[FactoryOrderStatus.SHIPPED] == frozenset()
        assert FactoryOrderStatus.SHIPPED in FACTORY_ORDER_TERMINAL_STATUSES

    def test_no_backward_transitions(self) -> None:
        """不允许回退（``COMPLETED → PRODUCING`` / ``SHIPPED → COMPLETED`` 都不合法）。

        业务规则：回退会让「已完成」的工单重新接受上报，
        工厂可以靠回退把进度数字改小——而进度是对账依据。
        """
        assert (
            FactoryOrderStatus.PRODUCING
            not in FACTORY_ORDER_TRANSITIONS[FactoryOrderStatus.COMPLETED]
        )
        assert (
            FactoryOrderStatus.COMPLETED not in FACTORY_ORDER_TRANSITIONS[FactoryOrderStatus.SHIPPED]
        )

    def test_every_status_has_a_label(self) -> None:
        """每个状态都要有中文展示名。

        序列化器用的是 ``LABELS.get(status, order.status)``——缺一项不会报错，
        只会把英文状态码静默下发到工厂看板上。本用例把「缺标签」变成红灯。
        """
        for status in FactoryOrderStatus:
            assert status in FACTORY_ORDER_STATUS_LABELS, f"{status} 缺少中文展示名"
            assert FACTORY_ORDER_STATUS_LABELS[status]
        assert len(FACTORY_ORDER_STATUS_LABELS) == len(FactoryOrderStatus)

    def test_inspection_result_labels_cover_both_values(self) -> None:
        """抽检结果的两个取值都要有展示名（``resultLabel`` 直接来自本表）。"""
        assert set(INSPECTION_RESULT_LABELS) == {InspectionResult.PASS, InspectionResult.FAIL}
        assert INSPECTION_RESULT_LABELS[InspectionResult.PASS] == "合格"
        assert INSPECTION_RESULT_LABELS[InspectionResult.FAIL] == "不合格"
