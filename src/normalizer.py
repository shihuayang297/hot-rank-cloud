"""数据加工模块：TOP N 截取与最终记录组装。"""
from datetime import datetime

from .parser import HotItem


def take_top_n(items: list, top_n: int) -> list:
    """每个板块只保留排名前 N 的条目（按 rank 排序、去重）。"""
    by_platform: dict = {}
    for item in items:
        if item.rank < 1:
            continue
        by_platform.setdefault(item.platform, {})[item.rank] = item

    result = []
    for platform in sorted(by_platform):
        ranks = sorted(by_platform[platform])[:top_n]
        result.extend(by_platform[platform][r] for r in ranks)
    return result


def to_records(items: list, crawl_time: datetime = None) -> list:
    """转换为最终落库记录（与腾讯文档列一一对应）。"""
    crawl_time = crawl_time or datetime.now()
    time_str = crawl_time.strftime("%Y-%m-%d %H:%M:%S")
    return [
        {
            "platform": it.platform,
            "title": it.title,
            "rank": it.rank,
            "crawl_time": time_str,
            "hot_value": it.hot_value,
        }
        for it in items
    ]
