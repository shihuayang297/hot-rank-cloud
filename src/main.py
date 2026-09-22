"""热点榜单自动采集系统 - 单次采集入口。

用法:
    python -m src.main

流程:
    1. 加载配置
    2. 请求 rree.cn 数据接口（指数退避重试）
    3. 解析各平台榜单 -> 每平台截取 TOP3
    4. 写入本地 JSONL 备份 + latest_batch.json（供腾讯文档写入使用）
    5. 输出摘要

退出码: 0=成功  1=失败（定时任务可据此告警）
"""
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from .fetcher import Fetcher, FetchError
from .log import setup_logger
from .normalizer import take_top_n, to_records
from .parser import parse_payload
from .writers.local_backup import LocalBackupWriter

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 守护进程补跑错过的整点时，用这个环境变量指定批次时间戳（格式 %Y-%m-%d %H:%M:%S）
BATCH_TIME_ENV = "HOT_RANK_BATCH_TIME"


def load_config() -> dict:
    config_path = PROJECT_ROOT / "config" / "settings.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def batch_time(now: datetime, tolerance_min: int) -> datetime:
    """把采集时刻归一为「所属整点」。

    守护进程在 xx:00:00 触发，但采集本身要花几秒到几分钟，直接用完成时刻会得到
    12:00:01 / 12:03:47 这类零碎时间戳。落到文档和群推送里，批次时间应该表达
    「这批数据属于哪个整点」，所以在容差范围内对齐到最近的整点。

    容差外（例如手工在 11:41 跑一次）保留真实时间，避免把非整点的批次伪装成整点。
    """
    if now.minute < tolerance_min:                      # 刚过整点 -> 向下取整
        return now.replace(minute=0, second=0, microsecond=0)
    if now.minute >= 60 - tolerance_min:                # 将到整点 -> 向上取整
        return (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    return now


def resolve_stamp(collect_cfg: dict) -> datetime:
    """决定本批次的时间戳。

    优先级：环境变量（守护进程补跑指定）> 整点对齐 > 真实时间。
    """
    override = os.environ.get(BATCH_TIME_ENV)
    if override:
        try:
            return datetime.strptime(override, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass  # 格式非法则忽略，退回正常逻辑
    if collect_cfg.get("align_to_hour", True):
        return batch_time(datetime.now(), collect_cfg.get("align_tolerance_minutes", 5))
    return datetime.now()


def already_collected(data_dir: Path, stamp: datetime) -> bool:
    """该整点是否已经采过。

    云端 workflow 每小时排了两次（0 分 + 20 分兜底 Actions 排队延迟），
    正常情况下第二次会命中这里直接退出，不会产生重复批次。
    """
    f = data_dir / f"hot_{stamp:%Y%m%d}.jsonl"
    if not f.exists():
        return False
    target = stamp.strftime("%Y-%m-%d %H:%M:%S")
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            if json.loads(line).get("crawl_time") == target:
                return True
        except json.JSONDecodeError:
            continue
    return False


def run() -> int:
    config = load_config()
    logger = setup_logger(str(PROJECT_ROOT / config["storage"]["logs_dir"]))
    logger.info("========== 采集任务开始 ==========")

    data_dir = PROJECT_ROOT / config["storage"]["data_dir"]
    planned = resolve_stamp(config["collect"])
    if already_collected(data_dir, planned):
        logger.info("整点 %s 已有数据，跳过本轮（Actions 重复触发属正常）", planned)
        print(json.dumps({"status": "skipped", "reason": "already_collected",
                          "crawl_time": planned.strftime("%Y-%m-%d %H:%M:%S")},
                         ensure_ascii=False))
        return 0

    try:
        # 1. 采集
        fetcher = Fetcher(config)
        payload = fetcher.fetch_hot_payload()

        # 2. 解析
        parsed = parse_payload(payload, config)
        top_n = config["collect"]["top_n"]
        top_items = take_top_n(parsed.items, top_n)

        min_platforms = config["collect"].get("min_platforms", 1)
        if len(parsed.platforms) < min_platforms:
            logger.error(
                "成功解析板块数 %d 低于阈值 %d，本次数据视为无效，不落盘",
                len(parsed.platforms), min_platforms,
            )
            return 1

        # 3. 组装记录并本地备份
        #    时间戳在采集「之前」就定好（planned），避免慢响应跨过整点导致归属漂移
        stamp = planned
        records = to_records(top_items, crawl_time=stamp)
        writer = LocalBackupWriter(str(PROJECT_ROOT / config["storage"]["data_dir"]))
        written = writer.write(records)

        logger.info(
            "采集完成: %d 个板块 / %d 条TOP%d记录（失败板块: %s）",
            len(parsed.platforms), written, top_n,
            parsed.failed_platforms or "无",
        )
        if parsed.failed_platforms:
            logger.warning("部分板块解析失败，已在日志中记录，不影响整体数据")

        # 4. 控制台输出摘要（供定时任务读取）
        platforms = sorted({r["platform"] for r in records})
        print(json.dumps({
            "status": "ok",
            "crawl_time": records[0]["crawl_time"] if records else None,
            "platform_count": len(platforms),
            "record_count": written,
            "platforms": platforms,
            "latest_batch_file": str(PROJECT_ROOT / config["storage"]["data_dir"] / "latest_batch.json"),
        }, ensure_ascii=False, indent=1))
        return 0

    except FetchError as e:
        logger.error("网络采集失败: %s", e)
        return 1
    except ValueError as e:
        logger.error("解析失败（页面可能改版）: %s", e)
        return 1
    except Exception as e:  # 兜底，保证异常必然被记录
        logger.exception("采集任务发生未预期异常: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(run())
