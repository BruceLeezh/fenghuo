# -*- coding: utf-8 -*-
"""生成 `app/data/contracts.json` —— 合同关键条款登记表的**模拟数据**。

⚠️ 本文件生成的全部内容均为模拟数据，非真实合同，不得对外作为真实合同信息引用。
   行内提供真实合同后，用同样的结构替换即可，无需改动 core/contract.py。

设计意图（让"同一条款缺失、不同暴露度"在数据里可见）：
  S001 科蓝   手机银行/网上银行（重要）× 4 项高影响缺口（含 3 项知识产权）→ high
  S002 恒生   核心账务系统（重要）× 3 项高影响缺口（含源代码托管）      → high
  S003 宇信   信贷系统（中等）   × 同样缺源代码托管，但条线重要性中等    → medium
  S004 长亮   核心系统/支付系统（重要）× 只有一般缺口，不含高影响条款    → low
  S005 京北方 客服中心/集中作业（中等）× 2 项一般缺口                    → low
  S006 信雅达 影像平台（一般）  × 条款齐备                              → none
  S007 神州数码 数据中心运维（重要）× 2 项一般缺口                       → low
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.contract import CLAUSES, CONTRACT_PATH  # noqa: E402

MOCK_NOTE = "模拟数据·非真实合同"

# 每家：合同元信息 + 缺失条款 key 列表（其余条款视为已约定）
FIXTURES = [
    {
        "supplier_id": "S001",
        "contract_name": "手机银行与网上银行系统开发测试外包服务合同",
        "signed_at": "2023-06-01",
        "missing": ["source_escrow", "ip_ownership", "data_ownership",
                    "third_party_oss", "derivative_ownership", "deliverables",
                    "eval_linkage"],
    },
    {
        "supplier_id": "S002",
        "contract_name": "核心账务系统运行维护服务合同",
        "signed_at": "2022-01-01",
        "missing": ["source_escrow", "exit_transition", "incident_plan"],
    },
    {
        "supplier_id": "S003",
        "contract_name": "信贷系统开发测试外包服务合同",
        "signed_at": "2024-01-01",
        "missing": ["source_escrow", "deliverables", "renewal_eval"],
    },
    {
        "supplier_id": "S004",
        "contract_name": "核心系统与支付系统开发服务合同",
        "signed_at": "2023-09-01",
        "missing": ["derivative_ownership", "sla_penalty", "termination_notice"],
    },
    {
        "supplier_id": "S005",
        "contract_name": "客服中心与集中作业业务支持服务合同",
        "signed_at": "2024-04-01",
        "missing": ["sla_penalty", "eval_linkage"],
    },
    {
        "supplier_id": "S006",
        "contract_name": "影像平台运行维护服务合同",
        "signed_at": "2023-03-01",
        "missing": [],
    },
    {
        "supplier_id": "S007",
        "contract_name": "数据中心与网络运维服务合同",
        "signed_at": "2022-07-01",
        "missing": ["data_security", "onsite_cycle"],
    },
]


def build():
    out = {}
    for fx in FIXTURES:
        miss = set(fx["missing"])
        clauses, positions = {}, {}
        for c in CLAUSES:
            k = c["key"]
            if k in miss:
                clauses[k] = "无"
                positions[k] = "全文未检索到对应约定"
            else:
                clauses[k] = "有"
                positions[k] = f"第 {c['group']} 组相关条款已约定"
        out[fx["supplier_id"]] = {
            "contract_name": fx["contract_name"],
            "signed_at": fx["signed_at"],
            "clauses": clauses,
            "positions": positions,
            "data_note": MOCK_NOTE,
        }
    return out


if __name__ == "__main__":
    data = build()
    with open(CONTRACT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    total_missing = sum(len(fx["missing"]) for fx in FIXTURES)
    print(f"已生成 {CONTRACT_PATH}")
    print(f"{len(data)} 家供应商 × {len(CLAUSES)} 项条款，共 {total_missing} 项缺口（全部为模拟数据）")
