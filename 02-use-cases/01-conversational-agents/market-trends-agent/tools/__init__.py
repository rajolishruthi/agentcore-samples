"""
Market Trends Agent Tools

This package contains all the tools used by the market trends agent:
- browser_tool: AgentCore Browser Tool integration for web scraping
- broker_card_tools: Broker card parsing and market summary generation
- memory_tools: AgentCore Memory integration for broker profiles and conversation history
"""

from .broker_card_tools import (
    collect_broker_preferences_interactively,
    generate_market_summary_for_broker,
    get_broker_card_template,
    parse_broker_profile_from_message,
)
from .memory_tools import create_memory_tools, extract_actor_id, get_memory_from_ssm
from .skill_tools import (
    get_market_overview,
    get_sector_data,
    get_stock_data,
    read_skill,
    search_news,
)

__all__ = [
    "collect_broker_preferences_interactively",
    "create_memory_tools",
    "extract_actor_id",
    "generate_market_summary_for_broker",
    "get_broker_card_template",
    "get_market_overview",
    "get_memory_from_ssm",
    "get_sector_data",
    "get_stock_data",
    "parse_broker_profile_from_message",
    "read_skill",
    "search_news",
]
