"""写入层抽象：后续接入腾讯文档 Open API 时新增实现即可，采集层无需改动。"""
from abc import ABC, abstractmethod


class BaseWriter(ABC):
    @abstractmethod
    def write(self, records: list) -> int:
        """写入一批记录，返回成功写入条数。"""
        raise NotImplementedError
