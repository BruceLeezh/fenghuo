"""Mock 适配器 —— 读本地模拟数据，演示"即插即用"的最小实现。

后续接真实数据源（企查查/天眼查/裁判文书），只需照这个模板
写一个类，把真实 API 的返回翻译成统一 schema 即可。
"""
import json
import os

from core.schemas import (
    SupplierProfile, LegalEvent, NewsItem, FinancialMetrics,
)
from core.adapter import DataSourceAdapter, register

MOCK_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "mock")


@register("mock")
class MockAdapter(DataSourceAdapter):
    """读本地 JSON 的模拟数据源。"""

    def fetch_supplier(self, name: str) -> SupplierProfile:
        # 路径穿越防护：name 来自查询入参，需净化后拼接并做前缀校验，
        # 否则 `../../etc/passwd` 之类可越出 MOCK_DIR 读取任意 JSON。
        safe = os.path.basename((name or "").strip().replace("\\", "/"))
        if not safe or safe in (".", ".."):
            raise ValueError(f"非法的模拟数据名：{name!r}")
        base = os.path.realpath(MOCK_DIR)
        path = os.path.realpath(os.path.join(base, f"{safe}.json"))
        if os.path.commonpath([base, path]) != base:
            raise ValueError(f"非法的模拟数据路径：{name!r}")
        if not os.path.isfile(path):
            raise ValueError(f"模拟数据中未收录：{safe}")
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)

        legal = [LegalEvent(**e) for e in raw.get("legal_events", [])]
        news = [NewsItem(**n) for n in raw.get("news", [])]
        fin = FinancialMetrics(**raw["financials"]) if raw.get("financials") else None

        return SupplierProfile(
            name=raw["name"],
            important_outsourcing=raw.get("important_outsourcing", False),
            non_resident=raw.get("non_resident", False),
            cross_border=raw.get("cross_border", False),
            legal_events=legal,
            news=news,
            financials=fin,
        )
