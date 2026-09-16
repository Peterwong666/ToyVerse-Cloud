"""通用响应模型。

统一响应契约
------------
* 成功：直接返回业务数据（列表接口用 :class:`PageResult` 形状）
* 失败：``{"code", "message", "traceId", "details?"}``

错误响应不在此定义 Pydantic 模型——它由全局异常处理器直接构造，
以保证**任何**异常路径（含未捕获异常）的响应形状都一致。
"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ORMModel(BaseModel):
    """可从 ORM 对象直接构造的基类。"""

    model_config = ConfigDict(from_attributes=True)


class PageResult(BaseModel, Generic[T]):
    """分页结果。

    Python 侧使用 snake_case，对外序列化为 camelCase（FastAPI 默认 ``by_alias=True``）。
    """

    model_config = ConfigDict(populate_by_name=True, from_attributes=True)

    records: list[T] = Field(description="当前页记录")
    total: int = Field(description="总记录数")
    page: int = Field(description="当前页码")
    page_size: int = Field(alias="pageSize", description="每页条数")
    total_pages: int = Field(alias="totalPages", description="总页数")


class ListResult(BaseModel, Generic[T]):
    """不分页的列表结果（用于下拉选项等小数据集）。"""

    records: list[T]
    total: int


class MessageResult(BaseModel):
    """仅返回提示信息的响应。"""

    success: bool = True
    message: str = "操作成功"


class StatusToggleRequest(BaseModel):
    """启用/停用请求体。"""

    status: str = Field(description="目标状态")
    reason: str | None = Field(default=None, max_length=256, description="变更原因")
