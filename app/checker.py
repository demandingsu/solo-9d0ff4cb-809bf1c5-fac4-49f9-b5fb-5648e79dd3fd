"""套件 / 器件双向核对。

判定口径：
- 缺记录：套件列出了某器件编号，但完整抓取后目录中不存在该编号的器件详情。
- 归属不符：器件标注的所属套件与“列入它的套件”对不上（含所属套件未收录、
  套件未列入该器件、器件未注明所属套件、编号与详情页不符等）。
- 未被套件列入：器件既未注明所属套件，也没有任何套件将其列入。
- 无法确认（抓取失败）：存在抓取失败 / 抓取截断时，未找到的器件编号不直接判为缺记录。
- 无法确认（目录更新）：采集后复读发现页面变化或复读失败时，本轮可能混有两个
  版本的目录，标记为无法确认，不展示确定的缺记录、归属不符、未被套件列入及一致数量。
"""
from __future__ import annotations

from datetime import datetime

from models import CrawlResult, ItemRecord, SuiteRecord


def cross_check(result: CrawlResult) -> dict:
    suites: list[SuiteRecord] = result.suites
    items: list[ItemRecord] = result.items
    incomplete = bool(result.failures) or bool(result.notes)
    unstable = bool(result.recheck)  # 采集途中目录发生变化或复读失败
    failed_urls = {f.url for f in result.failures}

    items_by_no: dict[str, ItemRecord] = {}
    items_by_url: dict[str, ItemRecord] = {}
    number_conflicts: list[dict] = []
    for it in items:
        if it.number in items_by_no:
            number_conflicts.append(
                {"number": it.number, "kind": "器件", "first": items_by_no[it.number], "second": it}
            )
        else:
            items_by_no[it.number] = it
        items_by_url.setdefault(it.url, it)
    suites_by_no: dict[str, SuiteRecord] = {}
    for s in suites:
        if s.number in suites_by_no:
            number_conflicts.append(
                {"number": s.number, "kind": "套件", "first": suites_by_no[s.number], "second": s}
            )
        else:
            suites_by_no[s.number] = s

    missing: list[dict] = []       # 缺记录
    mismatches: list[dict] = []    # 归属不符
    unverifiable: list[dict] = []  # 无法确认（因抓取失败）
    orphans: list[ItemRecord] = [] # 未被套件列入
    listed_by: dict[str, list[SuiteRecord]] = {it.number: [] for it in items}
    member_status: dict[tuple[str, str], tuple[str, ItemRecord | None]] = {}
    consistent_pairs = 0

    # —— 正向：以套件列出的器件编号为纲，逐一核对 ——
    for s in suites:
        for m in s.members:
            key = (s.url, m.accession)
            if m.link and m.link in failed_urls:
                unverifiable.append(
                    {"suite": s, "accession": m.accession, "link": m.link,
                     "reason": "器件详情页抓取失败，无法核对"}
                )
                member_status[key] = ("无法确认", None)
                continue
            target = items_by_url.get(m.link) if m.link else None
            if target is not None and target.number != m.accession:
                mismatches.append(
                    {"problem": f"套件所列编号「{m.accession}」与详情页实际编号「{target.number}」不一致",
                     "suite": s, "item": target, "caveat": False}
                )
                listed_by.setdefault(target.number, []).append(s)
                member_status[key] = ("编号不符", target)
                continue
            if target is None:
                target = items_by_no.get(m.accession)
            if target is None:
                if incomplete:
                    unverifiable.append(
                        {"suite": s, "accession": m.accession, "link": m.link,
                         "reason": "已抓取页面中未找到该编号；因存在抓取失败/截断，无法确认是否漏录"}
                    )
                    member_status[key] = ("无法确认", None)
                else:
                    missing.append({"suite": s, "accession": m.accession})
                    member_status[key] = ("缺记录", None)
                continue
            listed_by.setdefault(target.number, []).append(s)
            if target.suite_no is None:
                mismatches.append(
                    {"problem": "器件未注明所属套件，但该套件将其列入",
                     "suite": s, "item": target, "caveat": False}
                )
                member_status[key] = ("归属不符", target)
            elif target.suite_no != s.number:
                mismatches.append(
                    {"problem": f"器件标注所属套件为「{target.suite_no}」，与列入它的套件不符",
                     "suite": s, "item": target, "caveat": False}
                )
                member_status[key] = ("归属不符", target)
            else:
                consistent_pairs += 1
                member_status[key] = ("一致", target)

    # —— 反向：以器件标注的所属套件为纲，逐一核对 ——
    for it in items:
        if it.suite_no:
            s = suites_by_no.get(it.suite_no)
            if s is None:
                mismatches.append(
                    {"problem": f"器件标注所属套件为「{it.suite_no}」，但目录中未抓取到该套件",
                     "suite": None, "item": it, "caveat": incomplete}
                )
            elif not any(
                mem.accession == it.number or (mem.link and mem.link == it.url)
                for mem in s.members
            ):
                mismatches.append(
                    {"problem": f"器件标注所属「{it.suite_no}」，但该套件未将其列入",
                     "suite": s, "item": it, "caveat": False}
                )
        if not listed_by.get(it.number) and it.suite_no is None:
            orphans.append(it)

    mismatch_item_urls = {row["item"].url for row in mismatches if row.get("item")}
    orphan_urls = {it.url for it in orphans}

    suite_rows = []
    for s in suites:
        members = []
        for m in s.members:
            status, target = member_status.get((s.url, m.accession), ("—", None))
            members.append({"acc": m.accession, "link": m.link, "status": status, "item": target})
        suite_rows.append({"suite": s, "members": members})

    item_rows = []
    for it in items:
        lbs = listed_by.get(it.number, [])
        if it.url in orphan_urls:
            status = "未被套件列入"
        elif it.url in mismatch_item_urls:
            status = "归属不符"
        elif lbs and it.suite_no and any(s.number == it.suite_no for s in lbs):
            status = "一致"
        else:
            status = "—"
        item_rows.append({"item": it, "listed_by": lbs, "status": status})

    return {
        "start_url": result.start_url,
        "elapsed": result.elapsed,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "incomplete": incomplete,
        "unstable": unstable,
        "recheck": result.recheck,
        "rechecked": result.rechecked,
        "notes": result.notes,
        "failures": result.failures,
        "missing": missing,
        "mismatches": mismatches,
        "orphans": orphans,
        "unverifiable": unverifiable,
        "duplicates": result.duplicates,
        "number_conflicts": number_conflicts,
        "suite_rows": suite_rows,
        "item_rows": item_rows,
        "list_pages": result.list_pages,
        "stats": {
            "list_pages": len(result.list_pages),
            "suites": len(suites),
            "items": len(items),
            "declared": sum(len(s.members) for s in suites),
            "consistent": consistent_pairs,
        },
    }
