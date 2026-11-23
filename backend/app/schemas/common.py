"""應用程式共用的 Pydantic schema。"""

from typing import Generic, Optional, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PaginationParams(BaseModel):
    """分頁參數。"""

    page: int = Field(default=1, ge=1, description="頁碼")
    page_size: int = Field(default=100, ge=1, le=1000, description="每頁筆數")


class PaginationInfo(BaseModel):
    """分頁中繼資料。"""

    page: int = Field(description="Current page number, starting at 1.")
    page_size: int = Field(description="Maximum rows per page.")
    total_records: Optional[int] = Field(description="Rows matching the query on all pages.")
    total_pages: Optional[int] = Field(description="Number of pages at this page_size.")
    next_cursor: Optional[str] = Field(
        default=None, description="Unused by page-number pagination; always null."
    )


class PaginatedResponse(BaseModel, Generic[T]):
    """分頁回應包裝格式。"""

    success: bool = True
    data: list[T]
    pagination: PaginationInfo


class APIResponse(BaseModel, Generic[T]):
    """標準 API 回應包裝格式。"""

    success: bool = True
    data: Optional[T] = None
    message: Optional[str] = None
    errors: Optional[list[str]] = None
