"""数据源适配器基类 + 注册中心 —— "即插即用"的核心。

上层只调用统一接口，不关心底层数据源。
新增数据源 = 写一个继承 DataSourceAdapter 的类 + 注册到 REGISTRY。
"""
from abc import ABC, abstractmethod
from typing import Dict, Type

from core.schemas import SupplierProfile


class DataSourceAdapter(ABC):
    """所有数据源的统一接口。上层只认这个抽象。

    子类除 fetch_supplier 外，可选覆盖 is_available() / source_info()，
    让聚合层与配置中心据此判断"是否真正可用、是否权威、覆盖哪些字段"。
    """

    @abstractmethod
    def fetch_supplier(self, name: str) -> SupplierProfile:
        """返回统一格式的供应商档案（含法律事件/舆情/财务）。"""

    def is_available(self) -> bool:
        """是否真正可用（占位/未实现的适配器返回 False）。默认 True。"""
        return True

    def source_info(self) -> dict:
        """返回 {label, category, authoritative} 元信息，供前端展示。默认空。"""
        return {}


# 数据源注册中心：按配置动态加载
DATA_SOURCE_REGISTRY: Dict[str, Type[DataSourceAdapter]] = {}


def register(name: str):
    """装饰器：把适配器类注册进中心。"""
    def decorator(cls):
        DATA_SOURCE_REGISTRY[name] = cls
        return cls
    return decorator


def get_adapter(name: str) -> DataSourceAdapter:
    """按名称实例化适配器。换数据源只需改 name。"""
    if name not in DATA_SOURCE_REGISTRY:
        raise ValueError(f"未知数据源: {name}，可用: {list(DATA_SOURCE_REGISTRY)}")
    return DATA_SOURCE_REGISTRY[name]()
