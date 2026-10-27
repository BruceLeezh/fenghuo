"""LLM 客户端适配器 —— 与数据源同构的"即插即用"。

公网模型和本地私有化模型是两个可互换实现，上层调用不变。
密级路由：涉密文件走 LocalLLM，公开文件走 CloudLLM（DeepSeek-V4.1-Flash）。
"""
import os
from abc import ABC, abstractmethod
from typing import List, Dict

from llm.deepseek import DeepseekClient


class LLMClient(ABC):
    """统一 LLM 接口。上层只认这个，不关心模型在哪跑。"""

    @abstractmethod
    def extract_events(self, text: str) -> List[Dict]:
        """从非结构化文本抽取结构化事件（返回统一事件 dict 列表）。"""

    @abstractmethod
    def analyze_metrics(self, metrics: Dict) -> str:
        """分析脱敏后的财务指标，返回文字结论。"""


class CloudLLM(LLMClient):
    """公开文件用：调 DeepSeek-V4.1-Flash（OpenAI 兼容）。数据出网，仅限非涉密。"""

    def __init__(self):
        self._client = DeepseekClient()

    def extract_events(self, text: str) -> List[Dict]:
        try:
            return self._client.extract_events(text)
        except Exception as e:
            # 离线兜底：未配置 key 或网络失败时返回空，不阻断流水线
            return [{"title": f"LLM 未接入（{e}）", "severity": "medium"}]

    def analyze_metrics(self, metrics: Dict) -> str:
        try:
            return self._client.analyze_metrics(metrics)
        except Exception as e:
            return f"（LLM 未接入：{e}）"


class LocalLLM(LLMClient):
    """涉密文件用：本地私有化模型（Ollama/vLLM 跑 Qwen/Llama 开源版）。
    数据全程不出内网。"""

    def extract_events(self, text: str) -> List[Dict]:
        # 占位：实际调本地模型服务（如 http://127.0.0.1:11434/api/generate）
        return [{"title": "本地抽取示例", "severity": "medium"}]

    def analyze_metrics(self, metrics: Dict) -> str:
        return "本地模型分析结论"


# 密级路由：根据密级标签选择客户端
def get_llm_client(level: str) -> LLMClient:
    """按密级路由。level: public / internal / classified / top_secret。"""
    if level in ("public", "internal"):
        return CloudLLM()
    if level == "classified":
        return LocalLLM()
    # top_secret 不碰 AI，走纯规则引擎（见 security/desensitize.py 或评级引擎）
    raise ValueError(f"密级 {level} 应走纯规则引擎，不调用 LLM")
