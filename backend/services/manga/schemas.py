from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class PageOut(BaseModel):
    index: int
    image_url: str
    width: int | None = None
    height: int | None = None


class ChapterSummary(BaseModel):
    id: int
    number: int
    title: str | None = None
    page_count: int
    created_at: datetime | None = None


class ChapterDetail(ChapterSummary):
    pages: list[PageOut] = Field(default_factory=list)


class MangaSummary(BaseModel):
    id: int
    slug: str
    title: str
    author: str | None = None
    tags: list[str] = Field(default_factory=list)
    description: str | None = None
    cover_url: str | None = None
    chapter_count: int = 0
    page_count: int = 0
    source_chat_title: str | None = None
    updated_at: datetime | None = None


class MangaDetail(MangaSummary):
    chapters: list[ChapterSummary] = Field(default_factory=list)


class MangaListResponse(BaseModel):
    data: list[MangaSummary]
    page: int
    limit: int
    total: int
    total_pages: int


class ManualPublishRequest(BaseModel):
    title: str
    author: str | None = None
    tags: list[str] = Field(default_factory=list)
    chapter_title: str | None = Field(default=None, alias="chapterTitle")
    image_urls: list[str] = Field(..., alias="imageUrls")
    source_key: str | None = Field(default=None, alias="sourceKey")
    source_chat_id: str | None = Field(default=None, alias="sourceChatId")
    source_chat_title: str | None = Field(default=None, alias="sourceChatTitle")

    class Config:
        allow_population_by_field_name = True


class ErrorResponse(BaseModel):
    error: str
    detail: Any | None = None
