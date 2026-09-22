"""平台白名单过滤 —— 写出层的最后一道防线。

解析层（`src/parser.py`）已经按 `collect.include_platforms` 过滤过了，理论上
落盘的 jsonl 就只有白名单平台。这里再过滤一遍是**有意的冗余**，因为：

1. **历史数据**：2026-09-21 17:50 之前采的 jsonl 里有 33 个平台。重建文档、
   补写历史批次时都会读到它们，不在写出层拦就会把不要的平台again写进文档。
2. **异常轮次**：万一某轮采集跑的是旧代码（服务器 git pull 失败、Actions 兜底
   用了旧 workflow），落盘的数据会含非白名单平台。写出层拦住就不会污染文档和看板。

三个写出方共用这一份实现，避免各写一遍后逐渐漂移：
    gen_dashboard.py          → 看板 data.json（本机 + 服务器）
    tencent_doc.py            → 个人版腾讯文档（服务器经官方 API）
    prepare_doc_rows.py       → 企业版腾讯文档（Mac 经连接器）

⚠️ 匹配必须是**精确相等**，理由同 parser.py：白名单含「百度」，若做子串匹配会
   连带放进「百度游戏榜」「百度小说」「百度电影」「百度电视剧」。
"""
import os
from pathlib import Path

try:
    import yaml
except ImportError:                                  # pragma: no cover
    yaml = None

_HERE = Path(__file__).resolve().parent

# 配置查找顺序：环境变量 → 自己旁边（服务器/cloud-collector 布局）
# → 兄弟目录 hot-rank-collector（本机根目录脚本调用时的布局）
_CANDIDATES = (
    _HERE / "config" / "settings.yaml",
    _HERE.parent / "hot-rank-collector" / "config" / "settings.yaml",
)


def config_path() -> Path:
    env = os.environ.get("HOT_RANK_CONFIG")
    if env:
        return Path(env)
    for p in _CANDIDATES:
        if p.exists():
            return p
    return _CANDIDATES[0]


def load_whitelist(path=None) -> set:
    """读 collect.include_platforms。

    Returns:
        平台名集合；未配置、配置为空、读不到文件或缺 yaml 模块时返回空集合。
        **空集合表示不过滤**（全部放行），这样白名单是可选特性，
        配置缺失不会导致一条数据都写不出去。
    """
    if yaml is None:
        return set()
    p = Path(path) if path else config_path()
    try:
        cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return set()
    return set((cfg.get("collect") or {}).get("include_platforms") or ())


def keep(platform: str, whitelist: set) -> bool:
    """单个平台是否放行。空白名单 = 全放行。"""
    return (not whitelist) or platform in whitelist


def filter_records(records, whitelist=None):
    """过滤记录列表，返回 (保留的记录, 被丢弃的平台名集合)。

    records 是 dict 列表（jsonl 的每行 / latest_batch.json 的 records）。
    """
    wl = load_whitelist() if whitelist is None else whitelist
    if not wl:
        return list(records), set()
    kept, dropped = [], set()
    for r in records:
        name = r.get("platform") or ""
        if name in wl:
            kept.append(r)
        else:
            dropped.add(name)
    return kept, dropped
