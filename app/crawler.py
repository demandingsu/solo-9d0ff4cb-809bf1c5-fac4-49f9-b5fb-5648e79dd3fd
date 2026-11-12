"""沿同站分页抓取套件 / 器件详情页。

页面识别约定（样本站与真实目录站均按此解析）：
- 页面类型：<meta name="page-kind" content="list|suite|item">
- 列表页分页：<a rel="next" href="...">
- 详情链接：href 路径中含 /suite/ 或 /item/ 的同站链接
- 套件页：编号 <span class="suite-no">，器件列表 <ul class="members"> 中
  每 <li> 的 <span class="acc-no"> 为器件馆藏编号，<a> 为可选详情链接
- 器件页：编号 <span class="acc-no">，所属套件 <p class="field-suite"> 内的
  <span class="suite-no">（无此标记视为未注明所属套件）
"""
from __future__ import annotations

import time
from collections import deque
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from models import CrawlResult, DeclaredItem, FetchFailure, ItemRecord, SuiteRecord

USER_AGENT = "catalog-checker/1.0 (offline museum audit)"


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
            soup = BeautifulSoup(html, "html.parser")
            kind = page_kind(soup)
            if kind == "suite":
                rec = parse_suite(url, soup)
                if rec is None:
                    failures.append(
                        FetchFailure(url, first_ref(url), "套件页缺少 .suite-no 编号标记，无法解析")
                    )
                else:
                    suites.append(rec)
            elif kind == "item":
                rec = parse_item(url, soup)
                if rec is None:
                    failures.append(
                        FetchFailure(url, first_ref(url), "器件页缺少 .acc-no 编号标记，无法解析")
                    )
                else:
                    items.append(rec)
            else:
                # 列表页（或起始的未知页）：沿 rel=next 继续分页
                list_pages.append(url)
                for a in soup.select('a[rel="next"][href]'):
                    u = normalize_url(urljoin(url, a["href"]))
                    if same_site(self.start_url, u):
                        enqueue(u, url)
            # 任意页面都可能链接到套件 / 器件详情
            for a in soup.find_all("a", href=True):
                u = normalize_url(urljoin(url, a["href"]))
                if (
                    u.startswith(("http://", "https://"))
                    and same_site(self.start_url, u)
                    and looks_like_detail(u)
                ):
                    enqueue(u, url)

        duplicates = [
            {"url": u, "referrers": [r for r in refs if r]}
            for u, refs in referrers.items()
            if u in visited and len([r for r in refs if r]) > 1
        ]
        duplicates.sort(key=lambda d: d["url"])
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
        )
