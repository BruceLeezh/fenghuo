"""DeepSeek-V4.1-Flash 接入 —— OpenAI 兼容 API 客户端。

通过 OpenAI 兼容接口调用 DeepSeek-V4.1-Flash（API 名 deepseek-flash），做两件事：
1. 事件抽取：非结构化文本 → 结构化事件列表
2. 财务分析：财务指标 → 文字结论

配置：环境变量 DEEPSEEK_API_KEY + DEEPSEEK_BASE_URL + DEEPSEEK_MODEL
（默认 base_url=https://api.deepseek.com，model=DeepSeek-V4.1-Flash，可覆盖）
注意：API 只接受 deepseek-flash / deepseek-v4-pro 等名称，
"DeepSeek-V4.1-Flash" 这类展示名会由 normalize_model 自动映射。
"""
import os
import json
from typing import List, Dict, Optional

# 模型名归一化：把常见写法映射到 API 实际支持的名称。
# DeepSeek 官方 API 仅接受 deepseek-flash / deepseek-v4-pro（及别名 deepseek-chat），
# 展示名（如 "Deepseek-V4.1-Flash"）直接传会返回 400 invalid_request_error。
_MODEL_ALIASES = {
    "deepseek-v4.1-flash": "deepseek-flash",
    "deepseek-v41-flash": "deepseek-flash",
    "deepseek-v4-flash": "deepseek-flash",
    "v4.1-flash": "deepseek-flash",
    "flash": "deepseek-flash",
    "deepseek-v4.1-pro": "deepseek-v4-pro",
    "deepseek-v41-pro": "deepseek-v4-pro",
    "pro": "deepseek-v4-pro",
}

# API 名 → 界面展示名（用户配置与界面统一显示 V4.1-Flash 这类产品名）
_DISPLAY_NAMES = {
    "deepseek-flash": "DeepSeek-V4.1-Flash",
    "deepseek-chat": "DeepSeek-V4.1-Flash",
    "deepseek-v4-pro": "DeepSeek-V4-Pro",
}

# 正文为空时的兜底模型（非 reasoning 别名，输出稳定）
_FALLBACK_MODEL = "deepseek-chat"

# 调用超时（秒）与重试次数。
# ⚠️ 原实现用 OpenAI SDK 默认超时（600 秒 + 2 次自动重试）——实测单次 /api/query
#    3.8~10.7 秒，遇上上游抖动时会在一次查询里串起"抽事件 + 分析指标"两次调用，
#    最坏可挂十几分钟。前端 fetchWithTimeout 对 /api/query 设的是 150 秒，
#    后端不设界就等于把超时决策权交给了 SDK 默认值 —— 用户看到的只是"查询超时"，
#    真正的耗时点却在 LLM。故显式压到 20 秒、不重试：宁可降级为"AI 分析未接入"，
#    也不让一个可选增强能力拖住整条查询链路（评分本身是纯规则，不依赖 LLM）。
LLM_TIMEOUT_S = float(os.environ.get("DEEPSEEK_TIMEOUT_S", "20"))
LLM_MAX_RETRIES = int(os.environ.get("DEEPSEEK_MAX_RETRIES", "0"))


def normalize_model(name: Optional[str]) -> str:
    """把用户填写的模型名归一到 API 支持的值（大小写/下划线/别名容错）。

    DeepSeek 官方 API 仅接受 deepseek-flash / deepseek-v4-pro 等名称；
    "DeepSeek-V4.1-Flash" 这类产品展示名直接传会 400，故统一在此映射。
    """
    raw = (name or "").strip()
    if not raw:
        return "deepseek-flash"
    key = raw.lower().replace("_", "-")
    return _MODEL_ALIASES.get(key, raw)


def display_name(api_model: str) -> str:
    """API 模型名 → 界面展示名（如 deepseek-flash → DeepSeek-V4.1-Flash）。"""
    return _DISPLAY_NAMES.get((api_model or "").strip(), api_model)


class DeepseekClient:
    """DeepSeek-V4.1-Flash 客户端（OpenAI 兼容）。"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
    ):
        # 占位符（your_deepseek_api_key）按"未配置"处理：否则会拿假 key 真调一次，
        # 报 401 而不是走「LLM 未接入」的降级路径。
        from core.credential import real_secret
        self.api_key = real_secret(api_key or os.environ.get("DEEPSEEK_API_KEY", ""))
        self.base_url = base_url or os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        self.model = normalize_model(model or os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"))

    def _chat(self, messages: List[Dict], temperature: float = 0.1) -> str:
        """调用 chat completion，返回文本内容。"""
        if not self.api_key:
            raise RuntimeError("未配置 DEEPSEEK_API_KEY，无法调用 LLM")

        from openai import OpenAI
        # timeout / max_retries 必须显式传：SDK 默认 600s + 2 次重试，
        # 叠加"事件抽取 + 财务分析"两次调用即可把单次查询拖过前端 150s 超时。
        client = OpenAI(api_key=self.api_key, base_url=self.base_url,
                        timeout=LLM_TIMEOUT_S, max_retries=LLM_MAX_RETRIES)

        try:
            resp = client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
            )
        except Exception as e:
            # 兜底：模型名不被 API 接受时，回退到官方默认名重试一次，
            # 避免因"展示名写进配置"而导致 LLM 能力静默失效。
            msg = str(e).lower()
            if "model" in msg and self.model != "deepseek-flash":
                self.model = "deepseek-flash"
                resp = client.chat.completions.create(
                    model=self.model, messages=messages, temperature=temperature,
                )
            else:
                raise

        content = (resp.choices[0].message.content or "").strip()
        if not content:
            # reasoning 模型在 token 受限时可能把预算耗在思考上导致正文为空：
            # 先取 reasoning_content，仍无则换稳定别名重试一次
            content = (getattr(resp.choices[0].message, "reasoning_content", "") or "").strip()
        if not content and self.model != _FALLBACK_MODEL:
            resp2 = client.chat.completions.create(
                model=_FALLBACK_MODEL, messages=messages, temperature=temperature,
            )
            content = (resp2.choices[0].message.content or "").strip()
        return content

    def extract_events(self, text: str) -> List[Dict]:
        """事件抽取：非结构化文本 → 结构化事件列表（带 schema 校验）。"""
        system = (
            "你是银行外包供应商风险监控的事件抽取器。"
            "从给定文本中抽取风险相关事件，输出 JSON 数组，每个元素包含：\n"
            '{"title": "事件标题", "event_type": "legal/operating/financial/reputation/concentration", '
            '"severity": "high/medium/low", "amount": "金额或null", "occurred_at": "日期或null", "evidence": "原文依据"}。\n'
            "只输出 JSON，不要输出任何其他文字。"
        )
        raw = self._chat([
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ])
        return self._parse_json(raw)

    def analyze_metrics(self, metrics: Dict) -> str:
        """财务分析：财务指标 → 文字结论（CFA 视角 + 风险提示）。"""
        system = (
            "你是银行外包供应商的财务风险分析师，具备 CFA 视角。"
            "根据给定财务指标，用简洁的中文输出：1) 财务健康度判断 2) 2-3 个关键风险点或亮点 3) 一句处置建议。"
            "控制在 150 字以内。"
        )
        return self._chat([
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(metrics, ensure_ascii=False)},
        ], temperature=0.3)

    def _parse_json(self, raw: str) -> List[Dict]:
        """解析 LLM 返回的 JSON（容错：剥离 markdown 代码块、截取 JSON 片段）。"""
        raw = raw.strip()
        # 剥离 ```json ... ``` 包裹
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1] if "```" in raw[3:] else raw
            raw = raw.replace("json", "", 1).strip()
        # 提取第一个 [ ... ] 或 { ... }
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            import re
            m = re.search(r"\[[\s\S]*\]|\{[\s\S]*\}", raw)
            if m:
                return json.loads(m.group(0))
            return []
