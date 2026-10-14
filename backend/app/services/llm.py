"""大模型客户端：OpenAI 兼容接口，改 .env 配置即可切换厂商。"""
import logging

from openai import OpenAI

from ..config import get_settings

log = logging.getLogger(__name__)

PROMPT_TEMPLATE = """你是证券分析助手。股票 {name}({code}) 当前价 {price}，进入历史安全区间 [{zone_low}, {zone_high}]。
用不超过100字说明其进入区间的可能原因与主要风险，结尾附"非投资建议"。"""


def _client() -> OpenAI:
    s = get_settings()
    return OpenAI(base_url=s.llm_base_url, api_key=s.llm_api_key,
                  timeout=s.llm_timeout)


def summarize(code: str, name: str, price: float,
              zone_low: float, zone_high: float) -> str | None:
    s = get_settings()
    if not (s.llm_enabled and s.llm_api_key):
        return None
    try:
        resp = _client().chat.completions.create(
            model=s.llm_model,
            messages=[{"role": "user", "content": PROMPT_TEMPLATE.format(
                name=name, code=code, price=price,
                zone_low=zone_low, zone_high=zone_high)}],
            max_tokens=s.llm_max_tokens,
        )
        return resp.choices[0].message.content
    except Exception as e:
        log.warning("llm summarize failed: %s", e)
        return None
