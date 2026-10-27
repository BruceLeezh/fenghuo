"""脱敏模块 —— 敏感实体识别替换，两端本地闭环。

用在**公网通道**上：送进云端 LLM 之前先在本地脱敏，模型返回后再在本地还原。
脱敏与还原都只在本机完成，映射表 `mapping` 只活在本次请求的内存里，不过网。

当前调用点：`server.py::query`（两条出网路径都过一遍）
  - 财务指标 → `analyze_metrics`：逐字段脱敏，结论本地还原
  - 负面新闻标题 → `extract_events`：标题里含公司名与涉事金额，是真正的标识载体

涉密通道**不经过这里**：它不出网，脱敏没有对象；
它的边界由 `security/netguard.no_network()` 保证（出网即抛错）。
"""
import re
from typing import Dict, List


# 敏感实体模式（规则版，可替换为 NER 模型增强）
# 注意：phone 用边界约束，避免匹配到账户号等长数字串中间；
# account 用负向前后瞻而非 \b（\b 在中文后接数字时不生效）
_PATTERNS = {
    "company": re.compile(r"[\u4e00-\u9fa5]{2,20}(?:科技有限公司|股份有限公司|有限责任公司|集团|银行|公司)"),
    "amount": re.compile(r"\d+(?:\.\d+)?(?:万|亿)?元"),
    "account": re.compile(r"(?<!\d)\d{16,19}(?!\d)"),
    "phone": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
}


def desensitize(text: str) -> tuple:
    """脱敏：敏感实体替换为占位符。返回 (脱敏文本, 映射表)。

    映射表用于本地还原，绝不出网。
    """
    mapping: Dict[str, str] = {}
    counters = {"company": 0, "amount": 0, "account": 0, "phone": 0}

    def repl(kind):
        counters[kind] += 1
        placeholder = f"<{kind.upper()}{counters[kind]}>"
        return placeholder

    result = text
    for kind, pat in _PATTERNS.items():
        def make_repl(kind=kind):
            def _r(m):
                orig = m.group(0)
                ph = repl(kind)
                mapping[ph] = orig
                return ph
            return _r
        result = pat.sub(make_repl(), result)

    return result, mapping


def restore(text: str, mapping: Dict[str, str]) -> str:
    """还原：把占位符换回原文。仅在本地执行。"""
    for ph, orig in mapping.items():
        text = text.replace(ph, orig)
    return text


if __name__ == "__main__":
    sample = "XX科技有限公司与九江银行签订5000万元核心系统外包合同，账户6217000012345678。"
    masked, m = desensitize(sample)
    print("脱敏后:", masked)
    print("还原后:", restore(masked, m))
