"""解析模块：从 rree.cn 数据接口的 JS 载荷中提取各平台榜单。

接口返回形如：
    var lytoday = document.getElementById('lytoday');
    lytoday.insertAdjacentHTML('afterbegin',`<style>...</style>
      <div class="hot-card">
        <div class="hot-head">...<span class="hot-title">微博热搜</span>...</div>
        <li class="hot-list"><span class="hot-index">1</span>
            <a ...>标题</a><span class="hot-rank">108w</span>
            <span class="hot-tag">新</span></li>
        ...
      </div>...`)

解析策略：不依赖 JS 结构本身，直接在整个文本中检索 hot-card DOM 节点，
页面改版只要 card/list 结构不变即可继续工作。
"""
import logging
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

logger = logging.getLogger("hot_collector.parser")


@dataclass
class HotItem:
    """单条热点。"""
    platform: str          # 归一化后的平台名
    raw_platform: str      # 页面原始板块名
    title: str
    rank: int
    hot_value: str = ""    # 归一化后的热度值；无则空字符串
    url: str = ""


@dataclass
class ParseResult:
    items: list = field(default_factory=list)
    platforms: list = field(default_factory=list)      # 成功解析的原始板块名
    failed_platforms: list = field(default_factory=list)
    skipped_platforms: list = field(default_factory=list)   # 被白名单挡掉的板块


def _clean_text(text: str) -> str:
    """清理 nbsp 等空白字符。"""
    if not text:
        return ""
    return text.replace("\xa0", " ").strip()


def _repair_payload(payload: str) -> str:
    """修复接口载荷中的畸形 HTML。

    该站 img 标签未闭合，形如：
        <img src='...'<span class='hot-title'>抖音热点</span>
    导致后续 span 被解析进 img 的属性。补上缺失的 '>' 后再交给解析器。
    """
    payload = re.sub(r"(<img\b[^>]*?)(<\w)", r"\1>\2", payload)
    return payload


def parse_payload(payload: str, config: dict) -> ParseResult:
    """解析接口载荷，返回所有平台的全部条目（截取 TOP N 由上层负责）。"""
    collect_cfg = config["collect"]
    name_map = collect_cfg.get("platform_name_map", {})
    exclude = set(collect_cfg.get("exclude_platforms", []))
    # 白名单：只保留这些平台。空/缺省表示不启用白名单（全采）。
    include = set(collect_cfg.get("include_platforms") or ())

    soup = BeautifulSoup(_repair_payload(payload), "html.parser")
    result = ParseResult()

    cards = soup.select("div.hot-card")
    if not cards:
        raise ValueError("未找到任何 hot-card 节点，页面结构可能已改版")

    for card in cards:
        title_el = card.select_one(".hot-title")
        if title_el is None:
            continue
        raw_platform = _clean_text(title_el.get_text())

        # 命中排除名单的板块跳过（黄历、历史上的今天等非热点榜单）
        if any(word in raw_platform for word in exclude):
            continue

        display_name = name_map.get(raw_platform, raw_platform)

        # 白名单过滤：**精确匹配**，原始名或归一化名命中任一即通过。
        #
        # 为什么不能用子串匹配（exclude 那种 `word in raw_platform`）：
        # 白名单含「百度」，子串匹配会连带放进「百度游戏榜」「百度小说」
        # 「百度电影」「百度电视剧」，一个条目变五个平台。
        # 同理「腾讯新闻」会吃掉「腾讯新闻精选」。这两个是独立板块，
        # 必须靠 == 才能分清。回归测试见 tests/test_platform_filter.py。
        if include and raw_platform not in include and display_name not in include:
            result.skipped_platforms.append(raw_platform)
            continue

        items = card.select("li.hot-list")
        if not items:
            logger.warning("板块「%s」未解析到任何条目，可能已改版", raw_platform)
            result.failed_platforms.append(raw_platform)
            continue

        count = 0
        for li in items:
            index_el = li.select_one(".hot-index")
            link_el = li.find("a")
            if index_el is None or link_el is None:
                continue
            try:
                rank = int(_clean_text(index_el.get_text()))
            except ValueError:
                continue

            item = HotItem(
                platform=display_name,
                raw_platform=raw_platform,
                title=_clean_text(link_el.get_text()),
                rank=rank,
                url=link_el.get("href", ""),
            )
            hot_el = li.select_one(".hot-rank")
            if hot_el is not None:
                item.hot_value = normalize_hot_value(_clean_text(hot_el.get_text()))
            result.items.append(item)
            count += 1

        if count > 0:
            result.platforms.append(raw_platform)
            logger.info("板块「%s」解析 %d 条", raw_platform, count)

    if include:
        logger.info("白名单模式：命中 %d/%d 个平台，跳过 %d 个非白名单板块",
                    len(result.platforms), len(include), len(result.skipped_platforms))
        # 白名单里配了但页面上没解析出来的 —— 可能是数据源改版或板块下线。
        # 必须告警：否则白名单会静默地少写数据，而总平台数看起来"正常"。
        got = set(result.platforms) | {name_map.get(p, p) for p in result.platforms}
        for name in sorted(include - got):
            logger.warning("白名单平台「%s」本次未采到（页面上找不到或解析失败）", name)

    return result


def normalize_hot_value(text: str) -> str:
    """热度值归一化：'108w'->1080000，'9k'->9000，'123'->'123'。

    Returns:
        纯数字字符串；无法解析时返回原文，缺失时返回空字符串。
    """
    text = _clean_text(text)
    if not text:
        return ""
    try:
        low = text.lower()
        if low.endswith("w"):
            return str(int(float(low[:-1]) * 10000))
        if low.endswith("k"):
            return str(int(float(low[:-1]) * 1000))
        return str(int(float(text)))
    except (ValueError, TypeError):
        # 无法归一化的（如评分 8.2 等）保留原文
        return text
