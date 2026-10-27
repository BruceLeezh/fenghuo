"""单命令闭环入口 —— 不开网页也能跑通完整流水线（冒烟自检用）。

数据采集(适配器) → 规则打分 + AHP 定权 → 综合安全度 → 红黄绿等级 + 处置建议

用法：
    python pipeline.py                       # 默认走 mock（无需联网）
    python pipeline.py local 北京科蓝软件系统股份有限公司
    python pipeline.py qcc   北京科蓝软件系统股份有限公司
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.adapter import get_adapter
from core.rating import rate_supplier, level_to_action
from core.ahp import get_weights

# import 适配器模块以触发 @register 注册（新增适配器时在此处 import）
# ⚠️ 这些是**副作用导入**，删掉会导致 get_adapter() 找不到对应数据源，
#    不要因为"看起来没用到"就清理（已标 noqa: F401）。
from adapters import mock_adapter      # noqa: F401  自测基线
from adapters import local_adapter     # noqa: F401  本地预采集库（离线兜底）
from adapters import eastmoney_adapter  # noqa: F401  东方财富公开接口（免费实时）
from adapters import qcc_adapter       # noqa: F401  企查查（工商+财务+司法+股东）
from adapters import ifind_adapter     # noqa: F401  同花顺 iFinD
from adapters import wind_adapter      # noqa: F401  万得 Wind
from adapters import generic_adapter   # noqa: F401  自定义 HTTP 源


def run(adapter_name: str = "mock", supplier_name: str = "XX科技有限公司"):
    """跑通单家供应商的完整评级流水线，返回结构化结果。"""
    adapter = get_adapter(adapter_name)
    supplier = adapter.fetch_supplier(supplier_name)

    rating = rate_supplier(supplier)

    result = {
        "supplier": rating.supplier,
        "important_outsourcing": supplier.important_outsourcing,
        "total_score": rating.total_score,
        "level": rating.level,
        "action": level_to_action(rating.level),
        "dimensions": {
            dim: {
                "score": d["score"],
                "deducted": d["deducted"],
                # ⚠️ 键名是 "name"（scoring/performance 统一产出七要素），
                #    旧稿写成 "rule" 会 KeyError。
                "rules": [t["name"] for t in d.get("triggered", [])],
            }
            for dim, d in rating.dimension_scores.items()
        },
    }
    return result


if __name__ == "__main__":
    import json
    adapter_name = sys.argv[1] if len(sys.argv) > 1 else "mock"
    supplier_name = sys.argv[2] if len(sys.argv) > 2 else "XX科技有限公司"

    weights, cr = get_weights()
    print("AHP 权重:", weights, "| CR:", cr)
    print()
    print(json.dumps(run(adapter_name, supplier_name), ensure_ascii=False, indent=2))
