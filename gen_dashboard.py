#!/usr/bin/env python3
"""云端版：把最新采集批次转成 GitHub Pages 看板的 data.json。

和本机版 gen_dashboard.py 的差别只有路径：
    本机  hot-rank-collector/data/  ->  hot-rank-dashboard/data.json
    云端  data/                     ->  docs/data.json   （docs 是 Pages 的发布目录）

为什么看板也要搬到云端：本机是 MacBook Air，带走/关机后看板就停止更新；
而 Actions 采集完顺手生成 data.json 提交，Pages 自动发布，
整条「采集 → 看板」链路完全不依赖本机，且 Pages 是真公网地址，手机流量也能打开。
"""
import glob
import json
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"

# 平台白名单过滤（2026-09-21 起）。解析层已过滤，这里是写出层兜底：
# 万一某轮跑的是旧代码（git pull 失败、Actions 用了旧 workflow），
# latest_batch.json 里会含非白名单平台，拦在这里就不会污染公网看板。
try:
    from platform_filter import filter_records
except ImportError:                                  # 模块缺失时退化为不过滤
    def filter_records(records, whitelist=None):
        return list(records), set()


def total_batches() -> int:
    """累计批次数 = 所有 jsonl 中不重复的 crawl_time 数量"""
    times = set()
    for f in glob.glob(str(DATA_DIR / "hot_*.jsonl")):
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        times.add(json.loads(line)["crawl_time"])
                    except (json.JSONDecodeError, KeyError):
                        continue
    return len(times)


def to_int_or_none(v):
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return None


def main() -> int:
    latest = DATA_DIR / "latest_batch.json"
    if not latest.exists():
        print(json.dumps({"status": "skipped", "reason": "no latest_batch.json"},
                         ensure_ascii=False))
        return 0

    batch = json.loads(latest.read_text(encoding="utf-8"))

    # 白名单过滤。整批都被滤掉说明白名单配错了（平台名写错之类），
    # 那种情况下宁可保留原数据也不要产出空看板。
    records, dropped = filter_records(batch["records"])
    if not records:
        records, dropped = batch["records"], set()

    platforms = OrderedDict()
    for r in records:
        platforms.setdefault(r["platform"], []).append({
            "rank": r["rank"],
            "title": r["title"],
            "hot_value": to_int_or_none(r.get("hot_value")),
        })

    data = {
        "updated_at": batch["written_at"],
        "total_batches": total_batches(),
        "platforms": [{"name": n, "items": items} for n, items in platforms.items()],
    }

    if not DOCS_DIR.exists():
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
    target = DOCS_DIR / "data.json"

    # 【防回退保护】本机是公网看板的主推送方（见本机 push_dashboard.py）：
    # 本机在线时会直接把最新 data.json 推到 docs/，云端只是兜底。
    # 若云端自己的批次比线上已有的更旧（例如云端漏了 17:00 而本机已推 17:00），
    # 盲目覆盖就会把看板回退一小时。所以只在「确实更新」时才写。
    # 时间戳是 "YYYY-MM-DD HH:MM:SS" 定长格式，字符串比较等价于时间比较。
    if target.exists():
        try:
            existing = json.loads(target.read_text(encoding="utf-8")).get("updated_at")
        except (json.JSONDecodeError, OSError):
            existing = None
        if existing and existing >= data["updated_at"]:
            print(json.dumps({
                "status": "skipped",
                "reason": "线上看板数据不旧于本次批次，跳过以避免回退",
                "existing": existing,
                "candidate": data["updated_at"],
            }, ensure_ascii=False))
            return 0

    target.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    out = {
        "status": "ok",
        "updated_at": data["updated_at"],
        "total_batches": data["total_batches"],
        "platforms": len(data["platforms"]),
    }
    if dropped:
        out["filtered_out"] = sorted(dropped)
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
