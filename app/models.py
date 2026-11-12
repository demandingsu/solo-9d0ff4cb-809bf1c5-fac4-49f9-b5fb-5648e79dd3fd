"""数据模型：套件、器件、抓取失败与抓取结果。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DeclaredItem:
    """套件页中列出的一件器件（编号 + 可选的详情页链接）。"""

    accession: str
    link: str | None = None


@dataclass
class SuiteRecord:
    """套件详情页。"""

    number: str
    name: str
    url: str
    members: list[DeclaredItem] = field(default_factory=list)


@dataclass
class ItemRecord:
    """器件详情页。"""

    number: str
    name: str
    url: str
    suite_no: str | None = None  # 器件页标注的所属套件编号，未注明为 None


@dataclass
class FetchFailure:
    """一次失败的详情/列表抓取。"""

    url: str
    referrer: str | None
    reason: str


@dataclass
class CrawlResult:
    start_url: str
    list_pages: list[str]
    suites: list[SuiteRecord]
    items: list[ItemRecord]
    failures: list[FetchFailure]
    duplicates: list[dict]  # {"url": ..., "referrers": [...]} 跨页重复引用（已去重）
    notes: list[str]        # 抓取截断等提示
    elapsed: float
