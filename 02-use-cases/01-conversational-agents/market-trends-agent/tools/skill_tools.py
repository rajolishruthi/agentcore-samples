"""LangGraph tools for loading and executing Market Trends skills."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from .browser_tool import get_stock_data as _browser_get_stock_data
from .browser_tool import search_news as _browser_search_news

_AGENT_ROOT = Path(__file__).resolve().parent.parent
_SKILL_PATHS = {
    "skills/earnings-snapshot/SKILL.md",
    "skills/portfolio-risk/SKILL.md",
    "skills/sector-rotation/SKILL.md",
    "skills/trend-analysis/SKILL.md",
}

_STOCK_DATA: dict[str, dict[str, Any]] = {
    "NVDA": {
        "company_name": "NVIDIA Corporation",
        "price": 182.67,
        "change_pct": 2.34,
        "pe_ratio": 45.2,
        "dividend_yield_pct": 0.03,
        "market_cap": "$4.46T",
        "market_cap_tier": "Mega",
        "sector": "Technology",
    },
    "TSLA": {
        "company_name": "Tesla, Inc.",
        "price": 329.65,
        "change_pct": -1.72,
        "pe_ratio": 174.8,
        "dividend_yield_pct": 0.0,
        "market_cap": "$1.06T",
        "market_cap_tier": "Mega",
        "sector": "Consumer Discretionary",
    },
    "JPM": {
        "company_name": "JPMorgan Chase & Co.",
        "price": 289.41,
        "change_pct": 0.81,
        "pe_ratio": 14.1,
        "dividend_yield_pct": 1.93,
        "market_cap": "$796B",
        "market_cap_tier": "Large",
        "sector": "Financials",
    },
    "LLY": {
        "company_name": "Eli Lilly and Company",
        "price": 921.34,
        "change_pct": 1.4,
        "pe_ratio": 52.8,
        "dividend_yield_pct": 0.58,
        "market_cap": "$875B",
        "market_cap_tier": "Large",
        "sector": "Healthcare",
    },
}

_SECTOR_DATA: dict[str, dict[str, Any]] = {
    "technology": {
        "trend": "Outperforming (+1.24% today, +8.3% YTD)",
        "key_themes": [
            "AI infrastructure investment",
            "Semiconductor demand",
            "Cloud computing growth",
        ],
        "risks": [
            "Premium valuations",
            "Regulatory scrutiny",
            "Advanced-chip export restrictions",
        ],
        "outlook": "Bullish; prefer NVDA, MSFT, and GOOGL.",
        "top_holdings": ["NVDA", "MSFT", "AAPL", "GOOGL", "META"],
    },
    "healthcare": {
        "trend": "Neutral (+0.31% today, +2.1% YTD)",
        "key_themes": ["GLP-1 growth", "AI-assisted drug discovery"],
        "risks": ["Drug-price negotiation", "Patent expirations"],
        "outlook": "Selective; prefer LLY and defensive healthcare exposure.",
        "top_holdings": ["LLY", "UNH", "JNJ", "ABBV", "MRK"],
    },
    "financials": {
        "trend": "Improving (+0.67% today, +5.8% YTD)",
        "key_themes": ["Rate normalization", "Investment-banking recovery"],
        "risks": ["Commercial real-estate credit", "Higher capital requirements"],
        "outlook": "Cautiously optimistic; prefer JPM and GS.",
        "top_holdings": ["JPM", "BAC", "GS", "MS", "BLK"],
    },
    "energy": {
        "trend": "Underperforming (-1.12% today, -3.4% YTD)",
        "key_themes": ["Energy transition", "LNG infrastructure investment"],
        "risks": ["Oil-price compression", "Geopolitical supply disruptions"],
        "outlook": "Mixed; prefer diversified energy exposure.",
        "top_holdings": ["XOM", "CVX", "COP", "EOG", "SLB"],
    },
    "consumer_staples": {
        "trend": "Stable (+0.22% today, +1.9% YTD)",
        "key_themes": ["Defensive demand", "Input-cost normalization"],
        "risks": ["Private-label competition"],
        "outlook": "Defensive; focus on dividend growers such as KO and PG.",
        "top_holdings": ["KO", "PG", "PEP", "COST", "WMT"],
    },
    "consumer_discretionary": {
        "trend": "Mixed (-0.45% today, +3.1% YTD)",
        "key_themes": ["E-commerce growth", "Selective consumer spending"],
        "risks": ["Labor-market weakness", "EV competitive pressure"],
        "outlook": "Selective; prefer AMZN and remain cautious on pure EV plays.",
        "top_holdings": ["AMZN", "HD", "MCD", "SBUX", "TSLA"],
    },
}


@tool
def read_skill(path: str) -> str:
    """Read an approved Market Trends SKILL.md file.

    Args:
        path: Canonical relative path such as
            ``skills/trend-analysis/SKILL.md``.

    Returns:
        The complete skill document, including frontmatter and instructions.
    """
    if path not in _SKILL_PATHS:
        return "Error: path must identify an available Market Trends SKILL.md file."

    skill_file = (_AGENT_ROOT / path).resolve()
    skills_root = (_AGENT_ROOT / "skills").resolve()
    if not skill_file.is_relative_to(skills_root) or not skill_file.is_file():
        return "Error: requested skill is unavailable."
    return skill_file.read_text(encoding="utf-8")


@tool
def get_stock_data(symbol: str) -> str:
    """Get live stock data, with reference data when the browser source is unavailable."""
    normalized = symbol.strip().upper()
    live_result = str(_browser_get_stock_data.invoke({"symbol": normalized}))
    if not live_result.lower().startswith("error getting stock data"):
        return live_result

    reference = _STOCK_DATA.get(normalized)
    if reference is None:
        return live_result
    return json.dumps(
        {
            "symbol": normalized,
            **reference,
            "data_note": "Deterministic reference fallback; live browser source unavailable.",
        }
    )


@tool
def search_news(query: str) -> str:
    """Search live business news, with reference headlines on source failure."""
    live_result = str(_browser_search_news.invoke({"query": query}))
    if not live_result.lower().startswith("error searching"):
        return live_result

    lowered = query.lower()
    if "lly" in lowered or "eli lilly" in lowered or "earnings" in lowered:
        headlines = [
            "Eli Lilly raises its full-year outlook as diabetes and obesity medicine demand remains strong.",
            "LLY investment continues in manufacturing capacity for its incretin medicine portfolio.",
        ]
    elif "nvda" in lowered or "nvidia" in lowered:
        headlines = [
            "NVIDIA demand remains supported by AI infrastructure investment and data-center spending.",
            "Semiconductor investors monitor premium valuations and advanced-chip export restrictions.",
        ]
    elif any(term in lowered for term in ("rates", "inflation", "gdp")):
        headlines = [
            "Markets weigh moderating inflation against the path of interest rates.",
            "GDP growth remains positive while investors watch the timing of policy normalization.",
        ]
    else:
        headlines = ["Markets trade selectively as investors balance growth and valuation risks."]
    return json.dumps(
        {
            "query": query,
            "headlines": headlines,
            "data_note": "Deterministic reference fallback; live news source unavailable.",
        }
    )


@tool
def get_market_overview() -> dict[str, Any]:
    """Get reference index, sector, mover, and sentiment data for skill workflows."""
    return {
        "indices": {
            "S&P 500": {"level": 5234.18, "change_pct": 0.42, "ytd_pct": 7.8},
            "NASDAQ": {"level": 16421.54, "change_pct": 0.87, "ytd_pct": 9.3},
            "Dow Jones": {"level": 38742.15, "change_pct": 0.18, "ytd_pct": 4.1},
            "VIX": {"level": 14.23, "change_pct": -3.21},
        },
        "sector_performance_today": {
            "Technology": 1.24,
            "Healthcare": 0.31,
            "Financials": 0.67,
            "Energy": -1.12,
            "Consumer Discretionary": -0.45,
            "Consumer Staples": 0.22,
        },
        "top_gainers": ["NVDA", "AMZN"],
        "top_losers": ["TSLA", "XOM"],
        "market_sentiment": "Moderately Bullish",
        "data_note": "Deterministic reference data for this evaluation sample.",
    }


@tool
def get_sector_data(sector: str) -> dict[str, Any]:
    """Get reference trend, themes, risks, outlook, and holdings for a sector."""
    key = sector.lower().replace(" ", "_").replace("-", "_")
    data = _SECTOR_DATA.get(key)
    if data is None:
        return {"error": f"Unknown sector: {sector}", "available": sorted(_SECTOR_DATA)}
    return {
        "sector": key,
        **data,
        "data_note": "Deterministic reference data for this evaluation sample.",
    }
