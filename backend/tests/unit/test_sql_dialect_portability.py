"""跨方言可移植性守卫（单测，不起数据库）。

为什么需要这个文件
------------------
P9 实现运营指标时，``_date_between`` 写成了

    func.date(column).between(date_from.isoformat(), date_to.isoformat())

它在 **SQLite 上完全正常**（``func.date()`` 返回 TEXT，字符串比字符串），
因此本地全量测试与冒烟全绿；但在 **PostgreSQL 上直接报错**：

    asyncpg.exceptions.UndefinedFunctionError:
    operator does not exist: date >= character varying

——因为 PG 的 ``func.date()`` 返回真正的 ``date``，与 ``varchar`` 不可比较。

这类缺陷的麻烦之处在于：**SQLite 上永远复现不出来**，只有真跑一次 PG 才会暴露
（本项目是在「公开前真跑 PG」时由冒烟发现的）。所以这里不测行为、测**绑定参数的类型**——
它不需要数据库，却能把这一类「字符串冒充日期」的写法钉死。
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.dialects import postgresql, sqlite

from app.models.device import Device
from app.services.metrics_service import _date_between, resolve_range

DATE_FROM = date(2026, 9, 1)
DATE_TO = date(2026, 9, 17)


def _bind_params(clause: object, dialect: object) -> dict[str, object]:
    compiled = clause.compile(dialect=dialect, compile_kwargs={"literal_binds": False})  # type: ignore[attr-defined]
    return dict(compiled.params)


@pytest.mark.parametrize("dialect", [postgresql.dialect(), sqlite.dialect()], ids=["postgres", "sqlite"])
def test_date_between_binds_date_objects_not_strings(dialect: object) -> None:
    """★ 回归守卫：绑定的必须是 ``date``，不能是 ``str``。

    这条断言在「字符串冒充日期」的实现上会失败——也就是它存在的全部意义。
    """
    clause = _date_between(Device.activated_at, DATE_FROM, DATE_TO)
    params = _bind_params(clause, dialect)

    assert params, "`between` 应当产生绑定参数；没有说明被内联成了字面量"
    for name, value in params.items():
        assert not isinstance(value, str), f"参数 {name} 被绑成了字符串 {value!r}——PG 上会报类型错"
        assert isinstance(value, date), f"参数 {name} 应是 date，实际是 {type(value).__name__}"
    assert set(params.values()) == {DATE_FROM, DATE_TO}


@pytest.mark.parametrize("dialect", [postgresql.dialect(), sqlite.dialect()], ids=["postgres", "sqlite"])
def test_date_between_compiles_on_both_dialects(dialect: object) -> None:
    """两种方言都能编译出 BETWEEN，且用 DATE 提取而不是逐行比较时间戳。

    注意**不要**用「内联渲染里有没有引号」来判据：``literal_binds`` 下 SQLAlchemy 对两种方言
    都会把日期渲染成 ``'2026-09-01'`` 这样的未定类型字面量（PG 会按上下文把它转成 ``date``，
    因此是合法的）。能区分「绑 date」与「绑字符串」的只有**绑定参数的类型**，见上一个用例。
    """
    clause = _date_between(Device.activated_at, DATE_FROM, DATE_TO)
    sql = str(clause.compile(dialect=dialect, compile_kwargs={"literal_binds": True}))  # type: ignore[attr-defined]

    assert "BETWEEN" in sql.upper()
    assert "date(" in sql.lower(), sql


def test_resolve_range_returns_date_objects() -> None:
    """区间解析的返回值也必须是 ``date``（否则会在下游被当成字符串传进 SQL）。"""
    start, end = resolve_range(None, None)
    assert isinstance(start, date) and isinstance(end, date)
    assert end >= start
    assert not isinstance(start, str) and not isinstance(end, str)
