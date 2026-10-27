"""AHP 定权 —— 用层次分析法确定风险维度的权重。

判断矩阵依据 = 监管文件优先级 + 实务文献。
本模块实现了 AHP 的一致性检验（CR < 0.1），答辩时可完整讲清
"为什么履约表现权重最高"——服务水平下滑是《办法》里最常触发处置动作的异常（第27条）。

六维判断矩阵（2026-09 扩展）
----------------------------
在原有五维（外部可查的"底子"）之上，新增第六维 **履约表现 performance** ——
即第25条六类服务水平指标的评价结论，代表"这次外包实际干得怎么样"。

为什么把履约表现放在第一位（比法律风险还高）？
    因为本题是"信息科技**外包**风险监管"，核心是外包服务本身；
    工商/司法/财务是"准入门槛"性质的风险（不合格就不该进来），
    而履约表现是"合作质量"性质的风险（进来了也可能越做越差）。
    对一家已经通过准入的供应商，监管实务中最常触发处置动作的，
    恰恰是服务水平问题（第27条督促/约谈/暂停），而不是它突然被列为被执行人。
    因此本轮按 Saaty 1-9 标度，将 performance 定为最高优先级。

一致性检验说明：扩展为六维后仍需满足 CR < 0.1，本模块在模块加载时
不做强制校验，而是由 get_weights() 返回实际 CR，供前端与报告展示。

纯 Python 实现，零依赖（矩阵运算量极小，无需 numpy）。
"""
from typing import Dict, List, Optional


# 六大维度（顺序与判断矩阵一致）
DIMENSIONS = ["performance", "legal", "operating", "financial", "reputation", "concentration"]

DIMENSION_LABELS = {
    "performance": "履约表现",
    "legal": "法律风险",
    "operating": "经营风险",
    "financial": "财务风险",
    "reputation": "舆情风险",
    "concentration": "关联与集中度风险",
}

# 维度性质：base = 供应商"底子"（能不能合作）；service = 合作"表现"（干得好不好）
DIM_KIND = {
    "performance": "service",
    "legal": "base",
    "operating": "base",
    "financial": "base",
    "reputation": "base",
    "concentration": "base",
}

# AHP 判断矩阵（Saaty 1-9 标度）：矩阵[i][j] 表示维度 i 相对维度 j 的重要程度
# 履约表现 > 法律 > 经营 > 财务 > 舆情 > 关联集中度
# （前五维沿用原监管红线排序；performance 作为外包服务质量的核心，列为首位）
JUDGMENT_MATRIX: List[List[float]] = [
    [1,   2,   3,   4,   5,   6],    # performance 履约表现
    [0.5, 1,   2,   3,   4,   5],    # legal       法律风险
    [1/3, 0.5, 1,   2,   3,   4],    # operating   经营风险
    [0.25, 1/3, 0.5, 1,   2,   3],   # financial   财务风险
    [0.2, 0.25, 1/3, 0.5, 1,   2],   # reputation  舆情风险
    [1/6, 0.2, 0.25, 1/3, 0.5, 1],   # concentration 关联与集中度
]

# 随机一致性指标 RI（1-10 阶）
_RI = {1: 0, 2: 0, 3: 0.58, 4: 0.90, 5: 1.12, 6: 1.24,
       7: 1.32, 8: 1.41, 9: 1.45, 10: 1.49}


def _col_sums(matrix: List[List[float]]) -> List[float]:
    n = len(matrix)
    return [sum(matrix[i][j] for i in range(n)) for j in range(n)]


def compute_weights(matrix: List[List[float]] = JUDGMENT_MATRIX) -> List[float]:
    """特征向量法求权重（列归一化后取行平均）。"""
    n = len(matrix)
    col_sums = _col_sums(matrix)
    norm = [[matrix[i][j] / col_sums[j] for j in range(n)] for i in range(n)]
    return [sum(row) / n for row in norm]


def consistency_ratio(matrix: List[List[float]] = JUDGMENT_MATRIX) -> float:
    """计算一致性比率 CR = CI / RI。CR < 0.1 视为通过一致性检验。"""
    n = len(matrix)
    weights = compute_weights(matrix)
    # 最大特征值：lambda_max = mean((A·w)/w)
    lambda_max = 0.0
    for i in range(n):
        aw = sum(matrix[i][j] * weights[j] for j in range(n))
        lambda_max += (aw / weights[i])
    lambda_max /= n
    ci = (lambda_max - n) / (n - 1)
    ri = _RI.get(n, 1.49)
    return ci / ri


def get_weights() -> tuple:
    """返回 ({维度名: 权重}, CR 一致性比率)。"""
    weights = compute_weights()
    cr = consistency_ratio()
    return (
        {dim: round(w, 4) for dim, w in zip(DIMENSIONS, weights)},
        round(cr, 4),
    )


def renormalize(weights: Dict[str, float],
                available: Optional[List[str]] = None) -> Dict[str, float]:
    """在部分维度不可用（na）时重新归一化权重。

    场景：某供应商尚未录入服务水平数据 → performance 维度为 na。
    此时若仍按原权重要求 performance 得分，等于把缺失数据当作 0 分，
    会凭空压低综合分（失真）。正确做法是把 na 维度的权重按比例
    分配给其余可用维度，保持权重总和为 1。

    返回：归一化后的权重字典（仅含 available 中的维度）。
    """
    if available is None:
        available = list(weights.keys())
    picked = {k: v for k, v in weights.items() if k in available}
    total = sum(picked.values())
    if total <= 0:
        n = len(picked) or 1
        return {k: round(1.0 / n, 4) for k in picked}
    return {k: round(v / total, 4) for k, v in picked.items()}


if __name__ == "__main__":
    weights, cr = get_weights()
    print("AHP 六维权重：")
    for dim in DIMENSIONS:
        kind = "服务表现" if DIM_KIND[dim] == "service" else "企业底子"
        print(f"  {DIMENSION_LABELS[dim]:<12s} {weights[dim]:.4f}  [{kind}]")
    print(f"一致性比率 CR = {cr}（{'通过' if cr < 0.1 else '不通过'}，阈值 0.1）")
