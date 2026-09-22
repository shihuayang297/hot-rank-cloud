"""日志配置模块。"""
import logging
import sys
from datetime import datetime
from pathlib import Path


def setup_logger(logs_dir: str, name: str = "hot_collector") -> logging.Logger:
    """配置同时输出到控制台与滚动文件的 logger。"""
    logs_path = Path(logs_dir)
    logs_path.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(name)
    if logger.handlers:  # 避免重复初始化
        return logger
    logger.setLevel(logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 控制台
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    # 按天滚动的文件
    log_file = logs_path / f"collector_{datetime.now():%Y%m%d}.log"
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger
