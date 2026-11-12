"""FastAPI 入口：表单 + 核对报告。"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urljoin

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from checker import cross_check
from crawler import CrawlError, Crawler

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="馆藏成套器物核对工具")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# 容器内抓取时的主机改写规则："localhost:8001->sample-site:80;..."
# 展示与同源判断始终使用用户输入的原始地址，保证报告中的来源链接可在浏览器直接打开。
REWRITE_SPEC = os.environ.get("URL_FETCH_REWRITE", "")
SAMPLE_ENTRY = os.environ.get("SAMPLE_ENTRY", "http://localhost:8001/")


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "sample_entry": SAMPLE_ENTRY,
            "sample_broken": urljoin(SAMPLE_ENTRY, "broken-1.html"),
            "sample_mutable": urljoin(SAMPLE_ENTRY, "mutable/"),
        },
    )


@app.get("/check", response_class=HTMLResponse)
def check(request: Request, url: str = Query(..., min_length=1)):
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={"message": "请输入以 http:// 或 https:// 开头的藏品列表页地址。"},
            status_code=400,
        )
    crawler = Crawler(url, rewrite_spec=REWRITE_SPEC)
    try:
        result = crawler.run()
    except CrawlError as e:
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={"message": str(e)},
            status_code=502,
        )
    report = cross_check(result)
    return templates.TemplateResponse(
        request=request, name="report.html", context={"r": report}
    )


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
