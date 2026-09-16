"""工厂端序列化：客户名脱敏 + 工单字段白名单。

本模块是工厂端数据保护的**第二层**（第一层是
:func:`app.db.scope.factory_scoped` 的工厂作用域过滤）。

为什么是「字段白名单」而不是「复用时删掉敏感字段」
==================================================
工厂是跨租户角色，工单背后是某个品牌客户的订单。若工厂端的序列化写成
「拿平台端的 ``OrderResponse`` 再 ``del`` 掉金额与联系方式」，
那么**每一次需求扩散都是一次泄漏机会**：订单新增 ``contractNo``、
客户新增 ``taxNo`` 时，只要没人记得同步删除列表，新字段就会静默下发。
更糟的是这种错误不会报错，只会在某个客户的合同金额出现在工厂后台时
才被发现。

白名单的写法把这个过程反过来：

1. 工厂端的响应模型（:class:`app.schemas.factory.FactoryOrderResponse`）
   **只声明允许下发的字段**，未声明的字段在类型层面无法表达；
2. 序列化器**只用显式传参构造响应**（``FactoryOrderResponse(id=..., ...)``），
   不写 ``model_validate(order)``——后者会把 ORM 上和模型同名的字段
   自动搬运过去，「模型里恰好有个敏感字段」就再次变成泄漏；
3. :attr:`FactoryOrderSerializer.ALLOWED_FIELDS` 与响应模型的字段集合
   由单测断言完全一致，新增字段必须同时改三处（模型 / 序列化器 / 白名单）
   才能通过测试；
4. 客户名只有 **``customerNameMasked``** 这一个表达，脱敏在
   :func:`mask_customer_name` 一处完成——响应模型里根本没有
   ``customerName`` 这个字段，所以「忘了脱敏」写不出来。

:attr:`FactoryOrderSerializer.DENIED_FIELDS` 是给测试与文档看的反面对照表
（这些名字**不允许**出现在工厂端响应里），口径按**对外 JSON 字段名**给出，
便于直接拿去断言响应体。
"""

from __future__ import annotations

from typing import Final

from app.models.enums import FACTORY_ORDER_STATUS_LABELS, FactoryOrderStatus
from app.models.factory import FactoryOrder
from app.schemas.factory import FactoryOrderDetailResponse, FactoryOrderResponse

#: 脱敏用的星号
MASK_CHAR = "*"


def _first_alnum(text: str) -> str:
    """取首个「文字字符」（字母或数字）。

    为什么要跳过非文字字符：客户名里带包裹性标点是常态
    （``星辰玩具（演示租户）`` / ``Acme (中国) Ltd`` / ``-华东-``）。
    若机械地把「第一个字符」当首字符，脱敏结果会变成
    ``星********）``——尾部括号占据可见位，工厂端看起来像一串坏数据；
    更糟的是它把「这个名字后面跟着一段括号备注」这件事漏了出去。
    取「首个文字字符」后得到 ``星********户``，长度不变、可对账，也不泄漏形态。
    """
    return next((char for char in text if char.isalnum()), text[0])


def _last_alnum(text: str) -> str:
    """取末个「文字字符」（见 :func:`_first_alnum` 的说明）。"""
    return next((char for char in reversed(text) if char.isalnum()), text[-1])


def mask_customer_name(name: str | None) -> str:
    """客户名脱敏：首字符 + 星号 + 尾字符。

    规则（P6 用户明确要求，中英文一律按**字符**计）：

    * 长度 ≥ 3：``首个文字字符 + "*" * (len - 2) + 末个文字字符``
    * 长度 ≤ 2：只留 ``首字符 + "*"``
      （长度 1 时没有任何可遮的中间部分，长度 2 时若按 ``len - 2`` 算
      星号数会是 0，等于没脱敏——必须按这条特例走）
    * 空串 / ``None``：返回 ``""``

    **长度恒等**：``len(masked) == len(name)``（长度 ≥ 3 时）。星号个数始终是
    ``len - 2``，所以「首尾取哪个字符」只影响可读性，不影响长度——
    这是工厂端表格列宽与导出格式能依赖它的前提，也是幂等性成立的原因。

    Examples:
        >>> mask_customer_name("中国移动")
        '中**动'
        >>> mask_customer_name("星辰玩具（演示租户）")
        '星********户'
        >>> mask_customer_name("Acme")
        'A**e'
        >>> mask_customer_name("AB")
        'A*'
        >>> mask_customer_name("")
        ''

    Note:
        只脱敏**不加密**：保留首尾字符是为了让工厂能在对账时确认
        「这单确实是这个客户下的」（同一位客户的多张工单应当脱敏成同一个
        字符串），而中间部分全遮保证无法还原出完整名称。
    """
    if not name:
        return ""
    if len(name) <= 2:
        return f"{name[0]}{MASK_CHAR}"
    return f"{_first_alnum(name)}{MASK_CHAR * (len(name) - 2)}{_last_alnum(name)}"


def burn_progress_percent(burned_count: int, quantity: int) -> float:
    """烧录进度百分比（保留一位小数，上限 100）。

    ``quantity`` 为 0（脏数据或空工单）时返回 ``0.0`` 而不是抛
    ``ZeroDivisionError``：工单列表与工作台统计里出现一张数量为 0 的工单时，
    应当显示「0%」并让人继续看得到它，而不是整个页面 500。
    上限截断到 100 是为防御脏数据（``burned_count > quantity``）——
    进度条画到 120% 会让前端样式错位。
    """
    if quantity <= 0:
        return 0.0
    return round(min(burned_count / quantity, 1.0) * 100, 1)


class FactoryOrderSerializer:
    """工单脱敏序列化器（工厂端**所有**工单响应的唯一出口）。

    实例无状态，业务代码既可以用模块级单例（``factory_service`` 的做法），
    也可以自行实例化——不存在「必须先初始化」的顺序问题。
    """

    #: 允许出现在工厂端工单响应里的字段名（与
    #: :class:`app.schemas.factory.FactoryOrderResponse` 的 ``model_fields``
    #: 完全一致，由单测断言）。
    ALLOWED_FIELDS: Final[frozenset[str]] = frozenset(
        {
            "id",
            "factory_order_no",
            "order_no",
            "factory_id",
            "factory_name",
            "customer_name_masked",
            "product_model",
            "firmware_version",
            "quantity",
            "burned_count",
            "remaining",
            "progress_percent",
            "status",
            "status_label",
            "assigned_at",
            "due_at",
            "shipped_at",
            "created_at",
        }
    )

    #: **禁止**下发的字段名（对外 JSON 口径），供测试与文档对照。
    #: 这里列的是「本可以给、但不给工厂」的字段：金额、申请人联系方式、
    #: 租户与客户产品的**真实**名称。它们存在于订单域与租户域，
    #: 因此真正的防线是「工厂端模型根本没有这些字段」，本表用于断言这一点。
    DENIED_FIELDS: Final[frozenset[str]] = frozenset(
        {
            "unitPrice",
            "totalAmount",
            "applicantName",
            "applicantPhone",
            "contactName",
            "contactPhone",
            "email",
            "tenantName",
            "clientProductName",
        }
    )

    def to_response(
        self,
        order: FactoryOrder,
        *,
        factory_name: str | None,
        order_no: str | None,
        customer_name: str | None,
    ) -> FactoryOrderResponse:
        """ORM → 工单列表项。

        全部字段**显式传参**（不使用 ``model_validate(order)``）：ORM 上
        新增一个字段时，这里不会自动跟着带上——多写一行的成本，
        换的是「新增敏感字段不会静默泄漏」。

        Args:
            order: 工单 ORM 对象。
            factory_name: 工厂名称（列表批量查出的映射，避免 N+1）。
            order_no: 来源订单号；订单行缺失时为 ``None``。
            customer_name: **未脱敏**的客户名，本方法内部脱敏后放入
                ``customerNameMasked``。传 ``None`` 与空串等价。
        """
        progress = burn_progress_percent(order.burned_count, order.quantity)
        return FactoryOrderResponse(
            id=order.id,
            factory_order_no=order.factory_order_no,
            order_no=order_no,
            factory_id=order.factory_id,
            factory_name=factory_name,
            customer_name_masked=mask_customer_name(customer_name),
            product_model=order.product_model,
            firmware_version=order.firmware_version,
            quantity=order.quantity,
            burned_count=order.burned_count,
            remaining=order.remaining,
            progress_percent=progress,
            status=order.status,
            status_label=FACTORY_ORDER_STATUS_LABELS.get(
                FactoryOrderStatus(order.status), order.status
            ),
            assigned_at=order.assigned_at,
            due_at=order.due_at,
            shipped_at=order.shipped_at,
            created_at=order.created_at,
        )

    def to_detail(
        self,
        order: FactoryOrder,
        *,
        factory_name: str | None,
        order_no: str | None,
        customer_name: str | None,
    ) -> FactoryOrderDetailResponse:
        """ORM → 工单详情（在列表项基础上追加生产备注）。

        明细（烧录记录 / 抽检记录）由服务层另查后填入——序列化器只碰工单自身
        的字段，避免它变成一个「什么都查一遍」的对象。构建方式是把已脱敏的
        列表项**展开**再补字段：这样详情与列表的共有字段只有一处脱敏逻辑，
        不存在「详情忘了脱敏」的可能。
        """
        base = self.to_response(
            order,
            factory_name=factory_name,
            order_no=order_no,
            customer_name=customer_name,
        )
        return FactoryOrderDetailResponse(
            **base.model_dump(),
            production_note=order.remark,
        )


#: 模块级单例（序列化器无状态，重复实例化没有意义）
SERIALIZER = FactoryOrderSerializer()

__all__ = [
    "MASK_CHAR",
    "SERIALIZER",
    "FactoryOrderSerializer",
    "burn_progress_percent",
    "mask_customer_name",
]
