"""分页参数与响应封装。

统一所有列表接口的分页契约：

* 请求：``?page=1&pageSize=20``
* 响应：``{"records": [...], "total": 100, "page": 1, "pageSize": 20, "totalPages": 5}``
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastapi import Query

from app.core.errors import validation_error

#: 单页最大条数，防止一次性拉取过多数据
MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 20


@dataclass(slots=True)
class PageParams:
    """分页与排序参数。"""

    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE
    sort_by: str | None = None
    order: Literal["asc", "desc"] = "desc"

    @property
    def offset(self) -> int:
        """SQL OFFSET。"""
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        """SQL LIMIT。"""
        return self.page_size

    def apply_sort(self, stmt: Any, model: Any, *, default_column: Any) -> Any:
        """给查询语句附加排序。

        仅接受模型上真实存在的字段名，避免 SQL 注入与拼写错误导致的 500。
        """
        column = default_column
        if self.sort_by:
            candidate = getattr(model, self.sort_by, None)
            if candidate is None or not hasattr(candidate, "asc"):
                raise validation_error(f"不支持的排序字段：{self.sort_by}")
            column = candidate
        return stmt.order_by(column.desc() if self.order == "desc" else column.asc())


def page_params(
    page: Annotated[int, Query(ge=1, description="页码，从 1 开始")] = 1,
    page_size: Annotated[
        int, Query(ge=1, le=MAX_PAGE_SIZE, alias="pageSize", description="每页条数")
    ] = DEFAULT_PAGE_SIZE,
    sort_by: Annotated[str | None, Query(alias="sortBy", description="排序字段")] = None,
    order: Annotated[Literal["asc", "desc"], Query(description="排序方向")] = "desc",
) -> PageParams:
    """FastAPI 依赖：解析分页参数。"""
    return PageParams(page=page, page_size=page_size, sort_by=sort_by, order=order)


def page_response(
    records: list[Any],
    total: int,
    params: PageParams,
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造统一的分页响应体。"""
    total_pages = (total + params.page_size - 1) // params.page_size if total else 0
    payload: dict[str, Any] = {
        "records": records,
        "total": total,
        "page": params.page,
        "pageSize": params.page_size,
        "totalPages": total_pages,
    }
    if extra:
        payload.update(extra)
    return payload


def plain_list(records: list[Any], *, total: int | None = None) -> dict[str, Any]:
    """不分页的列表响应（用于下拉选项等小数据集）。"""
    return {"records": records, "total": total if total is not None else len(records)}
