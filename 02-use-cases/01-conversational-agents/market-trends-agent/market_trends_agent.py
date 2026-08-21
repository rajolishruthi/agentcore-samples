import logging
from datetime import datetime, timezone
from typing import Any

from opentelemetry.instrumentation.langchain import LangchainInstrumentor

# AgentCore's OTEL launcher configures the provider/exporter. Initialize the
# LangChain callbacks explicitly as a guarded fallback so LangGraph ToolNode
# executions are always exported as tool spans for AgentCore Evaluations.
_langchain_instrumentor = LangchainInstrumentor()
if not _langchain_instrumentor.is_instrumented_by_opentelemetry:
    _langchain_instrumentor.instrument()

from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Memory setup is now handled in tools/memory_tools.py


# Define the agent using LangGraph construction with AgentCore Memory
def create_market_trends_agent() -> Any:
    """Create and configure the LangGraph market trends agent with memory."""

    from langchain_aws import ChatBedrock
    from langchain_core.messages import HumanMessage, SystemMessage
    from langgraph.graph import MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode, tools_condition
    from tools import (
        collect_broker_preferences_interactively,
        create_memory_tools,
        generate_market_summary_for_broker,
        get_broker_card_template,
        get_market_overview,
        get_memory_from_ssm,
        get_sector_data,
        get_stock_data,
        parse_broker_profile_from_message,
        read_skill,
        search_news,
    )

    # Get memory from SSM (created during deployment)
    memory_client, memory_id = get_memory_from_ssm()

    # Create session ID for this conversation, but actor_id will be determined from user input
    session_id = f"market-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"

    # Default actor_id - will be updated when user identifies themselves
    default_actor_id = "unknown-user"

    # Initialize your LLM with Claude Haiku 4.5 using inference profile
    llm = ChatBedrock(
        model="global.anthropic.claude-haiku-4-5-20251001-v1:0",
        provider="anthropic",
        model_kwargs={"temperature": 0.1, "max_tokens": 4096},
    )

    # Create memory tools using the memory_tools module
    memory_tools = create_memory_tools(memory_client, memory_id, session_id, default_actor_id)

    # Bind tools to the LLM (market data tools + memory tools + conversational broker tools)
    tools = [
        read_skill,
        get_stock_data,
        search_news,
        get_market_overview,
        get_sector_data,
        parse_broker_profile_from_message,
        generate_market_summary_for_broker,
        get_broker_card_template,
        collect_broker_preferences_interactively,
    ] + memory_tools
    llm_with_tools = llm.bind_tools(tools)

    # System message optimized for Claude Sonnet 4 with Long-Term AgentCore Memory
    system_message = """You're an expert market intelligence analyst with deep expertise in financial markets, business strategy, and economic trends. You have advanced long-term memory capabilities to store and recall financial interests for each broker you work with.

    PURPOSE:
    - Provide real-time market analysis and stock data
    - Maintain long-term financial profiles for each broker/client
    - Store and recall investment preferences, risk tolerance, and financial goals
    - Deliver personalized investment insights based on stored broker profiles
    - Build ongoing professional relationships through comprehensive memory

    AVAILABLE TOOLS:
    
    Real-Time Market Data:
    - get_stock_data(symbol): Retrieves current stock prices, changes, and market data
    - search_news(query): Searches the runtime's default business-news source
    - get_market_overview(): Returns deterministic reference index, sector, and mover data for skill evaluation workflows
    - get_sector_data(sector): Returns deterministic reference trend, outlook, risk, and holdings data for a sector

    MARKET ANALYSIS SKILLS:
    Before a specialized analysis, load exactly one relevant skill with read_skill(path), then follow every required workflow step and reproduce its Output Format exactly. Do not add a preamble:
    - Price trend or momentum: skills/trend-analysis/SKILL.md
    - Sector allocation or rotation: skills/sector-rotation/SKILL.md
    - Earnings or valuation: skills/earnings-snapshot/SKILL.md
    - Portfolio concentration or risk: skills/portfolio-risk/SKILL.md
    Use the canonical path exactly, including the trailing /SKILL.md. Do not load a skill for a general market overview, broker-profile management, or unrelated conversation.
    
    Broker Profile Collection (Conversational):
    - parse_broker_profile_from_message(user_message): Parse structured broker profile from user input
    - generate_market_summary_for_broker(broker_profile, market_data): Generate tailored market summary
    - get_broker_card_template(): Provide template for broker profile format
    - collect_broker_preferences_interactively(preference_type): Guide collection of specific preferences
    
    Memory & Financial Profile Management:
    - list_conversation_history(): Retrieve recent conversation history
    - get_broker_financial_profile(): Retrieve long-term financial interests and investment profile for this broker
    - update_broker_financial_interests(interests_update): Store new financial interests or profile updates
    - identify_broker(user_message): Use LLM to identify broker from their message and get their actor_id
    
    MULTI-STRATEGY LONG-TERM MEMORY CAPABILITIES:
    - You maintain persistent financial profiles for each broker using multiple memory strategies:
      * USER_PREFERENCE: Captures broker preferences, risk tolerance, and investment styles
      * SEMANTIC: Stores financial facts, market analysis, and investment insights
    - Use identify_broker() to intelligently extract broker identity using LLM analysis
    - Always check get_broker_financial_profile() for returning brokers to personalize service
    - Use update_broker_financial_interests() when brokers share new preferences or interests
    - Build comprehensive investment profiles over time across multiple memory dimensions
    - LLM-based identity extraction ensures consistent broker identification across varied introductions
    - Memory strategies work together to provide rich, contextual financial intelligence
    
    BROKER PROFILE MANAGEMENT WORKFLOW:
    
    **CRITICAL: MANDATORY BROKER IDENTIFICATION FIRST**
    
    1. **MANDATORY First Step - Identify Broker**: 
       - IMMEDIATELY use identify_broker(user_message) when ANY user message contains:
         * Names, introductions, or "I'm [name]" 
         * Broker cards or profile information
         * Company names or roles
         * ANY identity information whatsoever
       - This returns the correct actor_id and checks for existing profiles
       - Use the returned actor_id for ALL subsequent memory operations
       - DO NOT proceed with any other actions until broker identification is complete
    
    2. **Check Existing Profile**:
       - After identification, use get_broker_financial_profile(actor_id) with the identified actor_id
       - If profile exists, acknowledge their stored preferences and personalize responses
       - If no profile exists, proceed to collect new profile information
    
    3. **Profile Collection**:
       - **For broker cards (Name: X, Company: Y, etc.)**: 
         * FIRST: identify_broker(user_message) to get actor_id
         * THEN: parse_broker_profile_from_message() to extract structured data
         * FINALLY: update_broker_financial_interests(parsed_profile, actor_id) to store
       - For missing info: use collect_broker_preferences_interactively()
       - For template: use get_broker_card_template()
       - ALWAYS store collected info with update_broker_financial_interests(info, actor_id)
    
    4. **Memory Operations**:
       - ALWAYS pass the identified actor_id to memory functions
       - get_broker_financial_profile(actor_id_from_identify_broker)
       - update_broker_financial_interests(info, actor_id_from_identify_broker)
       - This ensures consistent broker identity across all sessions
    
    3. **Market Analysis**:
       - Provide real-time stock data using get_stock_data()
       - Search for relevant market news using search_news() with appropriate news sources
       - Connect market events specifically to each broker's stored financial interests
       - Prioritize analysis of stocks/sectors in their profile
    
    4. **Professional Standards**:
       - Deliver institutional-quality analysis tailored to each broker's stored risk tolerance
       - Reference their specific investment goals and time horizons from their profile
       - Provide recommendations aligned with their stored investment style and preferences
       - Maintain professional relationships through consistent, personalized service
    
    **IMMEDIATE ACTION REQUIRED FOR EVERY MESSAGE:**
    Before doing ANYTHING else, check if the user message contains:
    - Names (Name: X, I'm X, My name is X)
    - Broker cards or profile information
    - Company/role information
    - Any identity markers
    
    If YES: IMMEDIATELY call identify_broker(user_message) as your FIRST action
    If NO: Proceed with normal market analysis
    
    CRITICAL: Always use the memory tools to maintain and reference broker financial profiles. This is essential for providing personalized, professional market intelligence services."""

    # Define the chatbot node with automatic conversation saving
    def chatbot(state: MessagesState) -> dict[str, Any]:
        raw_messages = state["messages"]

        # Remove any existing system messages to avoid duplicates
        non_system_messages = [msg for msg in raw_messages if not isinstance(msg, SystemMessage)]

        # Filter messages more carefully to preserve tool_use/tool_result pairs
        filtered_messages = []
        i = 0
        while i < len(non_system_messages):
            msg = non_system_messages[i]

            # Check if message has content (for regular messages)
            if (
                hasattr(msg, "content")
                and isinstance(msg.content, str)
                and msg.content.strip()
                or hasattr(msg, "tool_calls")
                and msg.tool_calls
                or hasattr(msg, "tool_call_id")
                and msg.tool_call_id
            ):
                filtered_messages.append(msg)
            # Check for content list with tool blocks
            elif hasattr(msg, "content") and isinstance(msg.content, list):
                # Keep messages with tool content blocks
                has_tool_content = any(
                    isinstance(block, dict) and block.get("type") in ["tool_use", "tool_result"]
                    for block in msg.content
                )
                if has_tool_content:
                    filtered_messages.append(msg)
                else:
                    # Check if any text blocks have content
                    has_text_content = any(
                        isinstance(block, dict) and block.get("type") == "text" and block.get("text", "").strip()
                        for block in msg.content
                    )
                    if has_text_content:
                        filtered_messages.append(msg)
                    else:
                        logger.warning(f"Filtered out empty message: {type(msg).__name__}")
            else:
                logger.warning(f"Filtered out empty message: {type(msg).__name__}")

            i += 1

        # Always ensure SystemMessage is first
        messages = [SystemMessage(content=system_message)] + filtered_messages

        # Get response from model with tools bound
        response = llm_with_tools.invoke(messages)

        # Save conversation to AgentCore Memory - let the agent handle actor_id through tools
        # The agent will use identify_broker() tool to get the correct actor_id when needed
        latest_user_message = next(
            (
                msg.content
                for msg in reversed(messages)
                if isinstance(msg, HumanMessage) and isinstance(msg.content, str)
            ),
            None,
        )

        if latest_user_message and isinstance(response.content, str) and response.content.strip():
            # Use default actor_id for conversation saving - the agent tools will handle proper identification
            conversation = [
                (latest_user_message, "USER"),
                (response.content, "ASSISTANT"),
            ]

            # Validate that all message texts are non-empty
            if all(msg[0].strip() for msg in conversation):
                try:
                    # Use session-based actor_id for general conversation, tools will handle broker-specific memory
                    session_actor_id = f"session_{session_id}"
                    memory_client.create_event(
                        memory_id=memory_id,
                        actor_id=session_actor_id,
                        session_id=session_id,
                        messages=conversation,
                    )
                    logger.info(f"Conversation saved to AgentCore Memory for session: {session_id}")
                except Exception as error:  # noqa: BLE001 - memory must not fail the response
                    logger.warning("Error saving conversation to memory: %s", error)

        # Return updated messages
        return {"messages": raw_messages + [response]}

    # Create the graph
    graph_builder = StateGraph(MessagesState)

    # Add nodes
    graph_builder.add_node("chatbot", chatbot)
    graph_builder.add_node("tools", ToolNode(tools))

    # Add edges
    graph_builder.add_conditional_edges(
        "chatbot",
        tools_condition,
    )
    graph_builder.add_edge("tools", "chatbot")

    # Set entry point
    graph_builder.set_entry_point("chatbot")

    # Compile the graph
    return graph_builder.compile()


# Lazily build the graph on the first invocation so AgentCore's HTTP server
# becomes healthy within the Runtime initialization window.
_agent: Any | None = None


def _invoke_market_trends_agent(user_input: str) -> Any:
    global _agent

    from langchain_core.messages import HumanMessage

    if _agent is None:
        _agent = create_market_trends_agent()
    response = _agent.invoke({"messages": [HumanMessage(content=user_input)]})
    return response["messages"][-1].content


@app.entrypoint
def market_trends_agent_runtime(payload: dict[str, Any]) -> Any:
    """Invoke the market trends agent in AgentCore Runtime."""
    user_input = payload.get("prompt")
    if not isinstance(user_input, str) or not user_input.strip():
        raise ValueError("payload.prompt must be a non-empty string")
    return _invoke_market_trends_agent(user_input)


def market_trends_agent_local(payload: dict[str, Any]) -> Any:
    """Invoke the market trends agent for local testing."""
    user_input = payload.get("prompt")
    if not isinstance(user_input, str) or not user_input.strip():
        raise ValueError("payload.prompt must be a non-empty string")
    return _invoke_market_trends_agent(user_input)


if __name__ == "__main__":
    app.run()
