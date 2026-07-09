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

    page: int
    page_size: int
    total_records: Optional[int]
    total_pages: Optional[int]
    next_cursor: Optional[str] = None


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
