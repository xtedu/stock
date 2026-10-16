"""大模型客户端：OpenAI 兼容接口，改 .env 配置即可切换厂商。"""
import json
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


COMPARE_MAX_TOKENS = 1600

COMPARE_PROMPT = """你是证券分析助手。以下是同一天的候选股票量化指标（来自日线/盘中快照、安全区间、基本面与资金流），表头为列名、每行一只股票：

注：资金流为东方财富按单笔成交金额分档推算的统计口径（主力净额 = 超大单 + 大单净额），并非真实席位数据；
表中资金流单位为万元，"当日主力净额"为当日累计值（盘中为开盘至今）；
MA20斜率%为MA20均线相对斜率（%/日，正值=拐头向上，负值=仍下行），trend_side为左侧/右侧标签（right=右侧确认=MA20斜率>0，left=左侧观察=MA20斜率<=0）；
"-" 表示该维度缺失、不要据此判断。

请逐只做「次日」的定性研判，并给出相对强弱。硬性要求：
1. 只做定性判断，禁止编造精确的上涨概率数字或百分比；
2. view 只能是「看多」「中性」「看空」之一；
3. reason 不超过 60 字，说明主要依据与风险；
4. support / resistance 为数值（支撑位 / 压力位，单位与现价一致），无法判断填 null；
5. confidence 只能是「高」「中」「低」；
6. overall 用一句话总结整体结论。

严格只输出 JSON（不要 markdown 代码块、不要多余文字），格式：
{"overall": "一句话总体结论", "items": [{"code": "600988", "view": "看多", "reason": "理由", "support": 32.5, "resistance": 38.0, "confidence": "中"}]}"""

_POS = {"above": "高于上沿", "in": "区间内", "below": "低于下沿"}


def _format_table(rows: list[dict]) -> str:
    head = ("代码 名称 现价 涨幅% 区间下沿 区间上沿 位置 MA250乖离% MA20斜率% 右侧状态 "
            "得分 ROE均值% 营收CAGR% 净利CAGR% PE5年分位% "
            "主力净额万 主力净占比% 超大单净占比% 近5日主力净额万")
    _SIDE = {"right": "右侧", "left": "左侧"}
    lines = [head]
    for r in rows:
        lines.append(" ".join(str(x if x is not None else "-") for x in (
            r.get("code"), r.get("name"), r.get("price"), r.get("change_pct"),
            r.get("zone_low"), r.get("zone_high"),
            _POS.get(r.get("zone_status"), "-"), r.get("ma250_dev"),
            r.get("ma20_slope_pct"), _SIDE.get(r.get("trend_side"), "-"),
            r.get("score"), r.get("roe_avg"), r.get("rev_cagr_3y"),
            r.get("profit_cagr_3y"), r.get("pe_percentile"),
            _wan(r.get("main_net")), r.get("main_net_pct"),
            r.get("super_net_pct"), _wan(r.get("main_net_5d")))))
    return "\n".join(lines)


def _wan(v) -> float | None:
    """资金流单位：元 → 万元（保留 1 位小数）。"""
    return round(v / 1e4, 1) if isinstance(v, (int, float)) else None


def _extract_json(text: str | None) -> dict | None:
    """兼容 markdown 代码块包裹，取出首个 JSON 对象。"""
    if not text:
        return None
    t = text.strip()
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(t[start:end + 1])
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def compare(rows: list[dict]) -> dict | None:
    """对比研判：返回 {"overall": str, "items": [...]}，失败返回 None。"""
    s = get_settings()
    if not (s.llm_enabled and s.llm_api_key) or not rows:
        return None
    try:
        resp = _client().chat.completions.create(
            model=s.llm_model,
            messages=[{"role": "user",
                       "content": COMPARE_PROMPT + "\n\n" + _format_table(rows)}],
            max_tokens=COMPARE_MAX_TOKENS,
        )
        return _extract_json(resp.choices[0].message.content)
    except Exception as e:
        log.warning("llm compare failed: %s", e)
        return None
