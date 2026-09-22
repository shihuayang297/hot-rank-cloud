"""本地 JSONL 备份写入器。

每次采集结果按天追加到 data/hot_YYYYMMDD.jsonl，
并同步刷新 data/latest_batch.json 供下游（腾讯文档写入、看板生成）读取。
即使腾讯文档写入失败，本地数据也不丢失。
"""
import json
import os
from datetime import datetime
from pathlib import Path


class LocalBackupWriter:
    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _batch_stamp(records: list) -> str:
        """本批次的时间戳，以记录自带的 crawl_time 为准。

        不能用 datetime.now()：守护进程补跑错过的整点时（例如 13:07 补 13:00 那轮），
        记录里的 crawl_time 是 13:00:00，而 now() 是 13:07。两者不一致会让看板显示
        13:07、文档显示 13:00，看起来像是「文档和看板没同步」。
        """
        return records[0].get("crawl_time") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def write(self, records: list) -> int:
        if not records:
            return 0

        stamp = self._batch_stamp(records)
        # 日文件按批次日期归档，而不是当前日期——跨天补跑（00:10 补前一天 23:00）
        # 必须落到 23:00 所属的那一天，否则那批数据会出现在错误的日期文件里。
        try:
            day = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").strftime("%Y%m%d")
        except ValueError:
            day = f"{datetime.now():%Y%m%d}"

        daily_file = self.data_dir / f"hot_{day}.jsonl"
        with open(daily_file, "a", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        # 刷新最近一批数据快照（供同步任务写腾讯文档、gen_dashboard 生成看板数据）
        latest = self.data_dir / "latest_batch.json"
        payload = {
            "written_at": stamp,
            "count": len(records),
            "records": records,
        }
        tmp = latest.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        os.replace(tmp, latest)  # 原子替换，避免读到半截文件
        return len(records)
