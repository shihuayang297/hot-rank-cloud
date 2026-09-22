"""HTTP 采集模块：负责请求 rree.cn 数据接口，带指数退避重试。"""
import time
import logging

import requests

logger = logging.getLogger("hot_collector.fetcher")


class FetchError(Exception):
    """多次重试后仍失败。"""


class Fetcher:
    def __init__(self, config: dict):
        src = config["source"]
        retry = config["retry"]
        self.base_url = src["base_url"].rstrip("/")
        self.data_endpoint = src["data_endpoint"]
        self.headers = {
            "User-Agent": src["user_agent"],
            "Referer": src["referer"],
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        self.request_timeout = retry["request_timeout"]
        self.connect_timeout = retry["connect_timeout"]
        self.max_retries = retry["max_retries"]
        self.backoff_base = retry["backoff_base_seconds"]
        self.session = requests.Session()
        self.session.headers.update(self.headers)

    def fetch_hot_payload(self) -> str:
        """获取榜单数据接口原始返回（JS 文本，内嵌 HTML 片段）。

        注意：该接口流式生成，响应耗时约 1~3 分钟，调用方需容忍长等待。
        """
        ts = int(time.time())
        url = f"{self.base_url}/{self.data_endpoint.format(ts=ts)}"

        last_error = None
        for attempt in range(1, self.max_retries + 1):
            try:
                logger.info("第 %d/%d 次请求数据接口: %s", attempt, self.max_retries, url)
                resp = self.session.get(
                    url,
                    timeout=(self.connect_timeout, self.request_timeout),
                )
                resp.raise_for_status()
                if len(resp.text) < 2000:
                    # 接口异常时可能返回极短的错误内容
                    raise FetchError(f"接口返回内容过短({len(resp.text)}字节)，疑似异常: {resp.text[:200]}")
                logger.info("接口返回 %d 字节", len(resp.text))
                return resp.text
            except (requests.RequestException, FetchError) as e:
                last_error = e
                if attempt < self.max_retries:
                    wait = self.backoff_base * (2 ** (attempt - 1))
                    logger.warning("请求失败: %s，%d 秒后重试", e, wait)
                    time.sleep(wait)
                else:
                    logger.error("请求失败且重试用尽: %s", e)

        raise FetchError(f"数据接口请求失败（已重试 {self.max_retries} 次）: {last_error}")
