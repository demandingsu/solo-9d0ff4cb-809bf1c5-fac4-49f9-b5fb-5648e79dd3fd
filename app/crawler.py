"""沿同站分页抓取套件 / 器件详情页，并在采集后复读核对。

页面识别约定（样本站与真实目录站均按此解析）：
- 页面类型：<meta name="page-kind" content="list|suite|item">
- 列表页分页：<a rel="next" href="...">
- 详情链接：href 路径中含 /suite/ 或 /item/ 的同站链接
- 套件页：编号 <span class="suite-no">，器件列表 <ul class="members"> 中
  每 <li> 的 <span class="acc-no"> 为器件馆藏编号，<a> 为可选详情链接
- 器件页：编号 <span class="acc-no">，所属套件 <p class="field-suite"> 内的
  <span class="suite-no">（无此标记视为未注明所属套件）

采集后复读：首轮抓取结束后，重新读取本轮所有成功抓取的页面（列表页、
套件及器件详情），对照分页链接、详情链接、馆藏编号与所属关系。若复读
失败或内容发生变化（目录在抓取途中被更新，本轮数据可能混有两个版本），
把问题地址记入 CrawlResult.recheck，由核对层将本轮标记为「无法确认」；
全部一致时核对结论沿用首轮结果。
"""
from __future__ import annotations

import time
from collections import deque
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from models import (
    CrawlResult,
    DeclaredItem,
    FetchFailure,
    ItemRecord,
    RecheckIssue,
    SuiteRecord,
)

USER_AGENT = "catalog-checker/1.0 (offline museum audit)"

KIND_LABELS = {"list": "列表页", "suite": "套件页", "item": "器件页"}


class CrawlError(Exception):
    """起始地址无法抓取，核对无法进行。"""


class FetchFailed(Exception):
    """单个页面抓取失败。"""


def normalize_url(url: str) -> str:
    """去掉 fragment 与默认端口，保证同一页面只算一次。"""
    url, _ = urldefrag(url)
    parts = urlsplit(url)
    netloc = parts.netloc
    try:
        if (parts.scheme == "http" and parts.port == 80) or (
            parts.scheme == "https" and parts.port == 443
        ):
            netloc = parts.hostname or netloc
    except ValueError:
        pass
    return urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))


def same_site(a: str, b: str) -> bool:
    try:
        pa, pb = urlsplit(a), urlsplit(b)
        return (pa.scheme, (pa.hostname or "").lower(), pa.port) == (
            pb.scheme,
            (pb.hostname or "").lower(),
            pb.port,
        )
    except ValueError:
        return False


def looks_like_detail(url: str) -> bool:
    path = urlsplit(url).path
    return "/suite/" in path or "/item/" in path


def parse_rewrite_rules(spec: str) -> list[tuple[str, str]]:
    """解析 "localhost:8001->sample-site:80;127.0.0.1:8001->sample-site:80"。"""
    rules = []
    for part in (spec or "").split(";"):
        part = part.strip()
        if "->" in part:
            src, dst = part.split("->", 1)
            rules.append((src.strip().lower(), dst.strip()))
    return rules


def apply_rewrite(url: str, rules: list[tuple[str, str]]) -> str:
    """仅改写实际抓取用的主机:端口；展示与同源判断仍用原始地址。"""
    if not rules:
        return url
    p = urlsplit(url)
    try:
        hostport = (p.hostname or "").lower() + (f":{p.port}" if p.port else "")
    except ValueError:
        return url
    for src, dst in rules:
        if hostport == src:
            return urlunsplit((p.scheme, dst, p.path, p.query, ""))
    return url


def page_kind(soup: BeautifulSoup) -> str | None:
    m = soup.find("meta", attrs={"name": "page-kind"})
    if m and m.get("content"):
        return m["content"].strip().lower()
    return None


def _text(el) -> str | None:
    return el.get_text(strip=True) if el else None


def parse_suite(url: str, soup: BeautifulSoup) -> SuiteRecord | None:
    number = _text(soup.select_one(".suite-no"))
    if not number:
        return None
    name = _text(soup.find("h1")) or number
    members: list[DeclaredItem] = []
    seen: set[str] = set()
    for li in soup.select("ul.members li"):
        acc = _text(li.select_one(".acc-no"))
        if not acc or acc in seen:
            continue
        seen.add(acc)
        a = li.find("a", href=True)
        link = normalize_url(urljoin(url, a["href"])) if a else None
        members.append(DeclaredItem(accession=acc, link=link))
    return SuiteRecord(number=number, name=name, url=url, members=members)


def parse_item(url: str, soup: BeautifulSoup) -> ItemRecord | None:
    number = _text(soup.select_one(".acc-no"))
    if not number:
        return None
    name = _text(soup.find("h1")) or number
    suite_no = None
    field = soup.select_one(".field-suite")
    if field is not None:
        sn = field.select_one(".suite-no")
        if sn:
            suite_no = sn.get_text(strip=True) or None
    return ItemRecord(number=number, name=name, url=url, suite_no=suite_no)


def describe_change(old: tuple, new: tuple) -> str:
    """说明同一页面两次读取快照的差异（快照结构见 Crawler._snapshot）。"""
    if old[0] != new[0]:
        return "页面类型由「{}」变为「{}」".format(
            KIND_LABELS.get(old[0], old[0]), KIND_LABELS.get(new[0], new[0])
        )
    kind = old[0]
    parts: list[str] = []
    if old[1] != new[1]:
        if kind == "suite":
            parts.append("套件编号或器件清单发生变化")
        elif kind == "item":
            parts.append("器件编号或所属套件标注发生变化")
        else:
            parts.append("页面解析结果发生变化")
    if old[2] != new[2]:
        parts.append("分页链接发生变化")
    if old[3] != new[3]:
        parts.append("详情链接发生变化")
    return "；".join(parts) or "页面内容发生变化"


class Crawler:
    def __init__(
        self,
        start_url: str,
        rewrite_spec: str = "",
        max_pages: int = 400,
        timeout: float = 8.0,
    ):
        self.start_url = normalize_url(start_url)
        self.rules = parse_rewrite_rules(rewrite_spec)
        self.max_pages = max_pages
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

    def _fetch(self, display_url: str) -> str:
        fetch_url = apply_rewrite(display_url, self.rules)
        try:
            resp = self.session.get(fetch_url, timeout=self.timeout)
        except requests.RequestException as e:
            raise FetchFailed(f"连接失败：{e.__class__.__name__}") from e
        if resp.status_code != 200:
            raise FetchFailed(f"HTTP {resp.status_code}")
        if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
            resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text

    def _parse_page(self, url: str, html: str):
        """解析一页，返回 (页面类型, 解析记录, 分页链接, 详情链接, 解析错误)。

        解析记录为 SuiteRecord / ItemRecord / None；列表页与解析失败的页面为 None。
        """
        soup = BeautifulSoup(html, "html.parser")
        kind = page_kind(soup)
        if kind not in ("suite", "item"):
            kind = "list"
        next_links: list[str] = []
        if kind == "list":
            for a in soup.select('a[rel="next"][href]'):
                u = normalize_url(urljoin(url, a["href"]))
                if same_site(self.start_url, u):
                    next_links.append(u)
        detail_links: list[str] = []
        for a in soup.find_all("a", href=True):
            u = normalize_url(urljoin(url, a["href"]))
            if (
                u.startswith(("http://", "https://"))
                and same_site(self.start_url, u)
                and looks_like_detail(u)
            ):
                detail_links.append(u)
        record: SuiteRecord | ItemRecord | None = None
        parse_error = None
        if kind == "suite":
            record = parse_suite(url, soup)
            if record is None:
                parse_error = "套件页缺少 .suite-no 编号标记，无法解析"
        elif kind == "item":
            record = parse_item(url, soup)
            if record is None:
                parse_error = "器件页缺少 .acc-no 编号标记，无法解析"
        return kind, record, next_links, detail_links, parse_error

    @staticmethod
    def _snapshot(kind, record, next_links, detail_links) -> tuple:
        """把与核对相关的内容压成可比较的快照，用于复读对照。"""
        if isinstance(record, SuiteRecord):
            rec = (record.number, tuple((m.accession, m.link) for m in record.members))
        elif isinstance(record, ItemRecord):
            rec = (record.number, record.suite_no)
        else:
            rec = None
        return (kind, rec, tuple(next_links), tuple(detail_links))

    def run(self) -> CrawlResult:
        t0 = time.monotonic()
        queue: deque[str] = deque([self.start_url])
        referrers: dict[str, list[str]] = {self.start_url: []}
        visited: set[str] = set()
        list_pages: list[str] = []
        suites: list[SuiteRecord] = []
        items: list[ItemRecord] = []
        failures: list[FetchFailure] = []
        notes: list[str] = []
        fetch_order: list[str] = []       # 本轮成功读取的页面（按抓取顺序）
        snapshots: dict[str, tuple] = {}  # 首轮内容快照，供复读对照

        def enqueue(u: str, ref: str | None) -> None:
            if u in visited:
                if ref is not None:
                    referrers.setdefault(u, []).append(ref)
                return
            if u in referrers:
                if ref is not None:
                    referrers[u].append(ref)
                return
            referrers[u] = [ref] if ref is not None else []
            queue.append(u)

        def first_ref(u: str) -> str | None:
            refs = referrers.get(u) or [None]
            return refs[0]

        while queue:
            if len(visited) >= self.max_pages:
                notes.append(f"达到抓取页数上限（{self.max_pages}），结果可能不完整")
                break
            url = queue.popleft()
            if url in visited:
                continue
            try:
                html = self._fetch(url)
            except FetchFailed as e:
                if url == self.start_url:
                    raise CrawlError(f"起始地址抓取失败：{e}") from e
                visited.add(url)  # 避免反复重试同一坏地址
                failures.append(FetchFailure(url=url, referrer=first_ref(url), reason=str(e)))
                continue
            visited.add(url)
            kind, record, next_links, detail_links, parse_error = self._parse_page(url, html)
            fetch_order.append(url)
            snapshots[url] = self._snapshot(kind, record, next_links, detail_links)
            if parse_error is not None:
                failures.append(FetchFailure(url, first_ref(url), parse_error))
            elif kind == "suite":
                suites.append(record)
            elif kind == "item":
                items.append(record)
            else:
                list_pages.append(url)
            for u in next_links:
                enqueue(u, url)
            for u in detail_links:
                enqueue(u, url)

        duplicates = [
            {"url": u, "referrers": [r for r in refs if r]}
            for u, refs in referrers.items()
            if u in visited and len([r for r in refs if r]) > 1
        ]
        duplicates.sort(key=lambda d: d["url"])

        # —— 采集后复读：重新读取本轮成功抓取的每一页，对照分页与详情链接、
        #    馆藏编号和所属关系，发现变化或复读失败即记录问题地址 ——
        recheck: list[RecheckIssue] = []
        for url in fetch_order:
            try:
                html = self._fetch(url)
            except FetchFailed as e:
                recheck.append(
                    RecheckIssue(url=url, referrer=first_ref(url), reason=f"复读抓取失败：{e}")
                )
                continue
            kind, record, next_links, detail_links, _ = self._parse_page(url, html)
            snap = self._snapshot(kind, record, next_links, detail_links)
            if snap != snapshots[url]:
                recheck.append(
                    RecheckIssue(
                        url=url,
                        referrer=first_ref(url),
                        reason=describe_change(snapshots[url], snap),
                    )
                )

        elapsed = time.monotonic() - t0
        return CrawlResult(
            start_url=self.start_url,
            list_pages=list_pages,
            suites=suites,
            items=items,
            failures=failures,
            duplicates=duplicates,
            notes=notes,
            elapsed=elapsed,
            recheck=recheck,
            rechecked=len(fetch_order),
        )
