from a2a.client import ClientConfig, ClientFactory
from a2a.types import TransportProtocol
from google.adk.agents.llm_agent import Agent
from google.adk.agents.remote_a2a_agent import RemoteA2aAgent
from google.adk.models.lite_llm import LiteLlm
from prompt import SYSTEM_PROMPT
from urllib.parse import quote
import httpx
import os
import uuid

IS_DOCKER = os.getenv("DOCKER_CONTAINER", "0") == "1"
# Use BEDROCK_MODEL_ID for Bedrock models via LiteLlm, or GOOGLE_MODEL_ID for Gemini
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "global.anthropic.claude-sonnet-4-20250514-v1:0")  # Claude on Bedrock
GOOGLE_MODEL_ID = os.getenv("GOOGLE_MODEL_ID", "gemini-2.5-flash")

if IS_DOCKER:
    from utils import get_ssm_parameter, get_aws_info
    from email_tool import send_email_tool
    from obo_token import fetch_obo_token
else:
    from host_adk_agent.utils import get_ssm_parameter, get_aws_info
    from host_adk_agent.email_tool import send_email_tool
    from host_adk_agent.obo_token import fetch_obo_token

# Gmail 3LO is enabled when the provider name is configured
GMAIL_PROVIDER_NAME = os.getenv("GMAIL_PROVIDER_NAME", "")
GMAIL_3LO_ENABLED = bool(GMAIL_PROVIDER_NAME)
print(f"DEBUG: GMAIL_PROVIDER_NAME={GMAIL_PROVIDER_NAME}, GMAIL_3LO_ENABLED={GMAIL_3LO_ENABLED}")


# AWS and agent configuration
account_id, region = get_aws_info()

# --- Monitor Agent (AWS AgentCore Runtime) ---
MONITOR_AGENT_ID = get_ssm_parameter("/monitoragent/agentcore/runtime-id")
MONITOR_AGENT_ARN = (
    f"arn:aws:bedrock-agentcore:{region}:{account_id}:runtime/{MONITOR_AGENT_ID}"
)

# --- WebSearch Agent (GCP Cloud Run) ---
WEBSEARCH_GCP_URL = os.getenv("WEBSEARCH_GCP_URL")  # e.g. https://web-search-agent-xxxxx-uc.a.run.app


class _OBOAuth(httpx.Auth):
    """httpx auth that injects an OBO-exchanged bearer per request."""

    requires_request_body = False

    def __init__(self, user_jwt: str):
        self._user_jwt = user_jwt

    async def async_auth_flow(self, request):
        token = await fetch_obo_token(self._user_jwt)
        request.headers["Authorization"] = f"Bearer {token}"
        yield request

    def sync_auth_flow(self, request):
        # ADK uses httpx.AsyncClient; this branch is only for completeness.
        raise RuntimeError("OBO auth requires async httpx client")


def _create_client_factory(session_id: str, actor_id: str, user_jwt: str):
    """Lazy A2A client factory. Each httpx request is authenticated with a
    fresh OBO-exchanged bearer (carries the onBehalfOf claim)."""

    def _get_authenticated_client() -> httpx.AsyncClient:
        headers = {
            "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Actorid": actor_id,
        }
        return httpx.AsyncClient(
            timeout=httpx.Timeout(timeout=300.0),
            headers=headers,
            auth=_OBOAuth(user_jwt),
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
        )

    class LazyClientFactory:
        def __init__(self):
            initial_client = _get_authenticated_client()
            base_config = ClientConfig(
                httpx_client=initial_client,
                streaming=False,
                supported_transports=[TransportProtocol.jsonrpc],
            )
            self._base_factory = ClientFactory(config=base_config)

        @property
        def _config(self):
            return self._base_factory._config

        @property
        def _registry(self):
            return self._base_factory._registry

        @property
        def _consumers(self):
            return self._base_factory._consumers

        def register(self, label, generator):
            return self._base_factory.register(label, generator)

        def create(self, agent_card):
            httpx_client = _get_authenticated_client()
            fresh_config = ClientConfig(
                httpx_client=httpx_client,
                streaming=False,
                supported_transports=[TransportProtocol.jsonrpc],
            )
            fresh_factory = ClientFactory(config=fresh_config)
            return fresh_factory.create(agent_card)

    return LazyClientFactory()


def get_root_agent(session_id: str, actor_id: str, user_jwt: str):
    # --- Monitor Agent (AWS AgentCore Runtime — unchanged) ---
    monitor_agent_card_url = (
        f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/"
        f"{quote(MONITOR_AGENT_ARN, safe='')}/invocations/.well-known/agent-card.json"
    )

    monitor_agent = RemoteA2aAgent(
        name="monitor_agent",
        description="Agent that handles monitoring tasks.",
        agent_card=monitor_agent_card_url,
        a2a_client_factory=_create_client_factory(
            session_id=session_id,
            actor_id=actor_id,
            user_jwt=user_jwt,
        ),
    )

    # --- WebSearch Agent (GCP Cloud Run — same OBO token, different URL) ---
    websearch_agent_card_url = f"{WEBSEARCH_GCP_URL}/.well-known/agent-card.json"

    websearch_agent = RemoteA2aAgent(
        name="websearch_agent",
        description="Web search agent for finding AWS solutions, documentation, and best practices.",
        agent_card=websearch_agent_card_url,
        a2a_client_factory=_create_client_factory(
            session_id=session_id,
            actor_id=actor_id,
            user_jwt=user_jwt,
        ),
    )

    # Select model: prefer Bedrock via LiteLlm if configured, else Gemini
    if BEDROCK_MODEL_ID:
        model = LiteLlm(model=f"bedrock/{BEDROCK_MODEL_ID}")
    else:
        model = GOOGLE_MODEL_ID

    # Create root agent
    tools_list = [send_email_tool] if GMAIL_3LO_ENABLED else []
    print(f"DEBUG: Creating root agent with {len(tools_list)} tools, GMAIL_3LO_ENABLED={GMAIL_3LO_ENABLED}")
    if tools_list:
        print(f"DEBUG: Tools: {[tool.__class__.__name__ for tool in tools_list]}")

    root_agent = Agent(
        model=model,
        name="root_agent",
        instruction=SYSTEM_PROMPT,
        sub_agents=[monitor_agent, websearch_agent],
        tools=tools_list,
    )

    return root_agent


async def get_agent_and_card(session_id: str, actor_id: str, user_jwt: str):
    """
    Lazy initialization of the root agent.
    This is called inside the entrypoint where workload identity is available.
    """

    root_agent = get_root_agent(
        session_id=session_id, actor_id=actor_id, user_jwt=user_jwt
    )

    async def get_agents_cards():
        agents_info = {}
        sub_agents = root_agent.sub_agents

        for agent in sub_agents:
            
            agent_data = {}

            # Access the source URL before resolution
            if hasattr(agent, "_agent_card_source"):
                agent_data["agent_card_url"] = agent._agent_card_source

            # Ensure resolution and access full agent card
            if hasattr(agent, "_ensure_resolved"):
                await agent._ensure_resolved()

                if hasattr(agent, "_agent_card") and agent._agent_card:
                    card = agent._agent_card
                    agent_data["agent_card"] = card.model_dump(exclude_none=True)

            agents_info[agent.name] = agent_data

        return agents_info

    # Get agents cards info
    agents_cards = await get_agents_cards()

    return root_agent, agents_cards


if not IS_DOCKER:
    session_id = str(uuid.uuid4())
    actor_id = "webadk"
    # Local dev path: no real user JWT available; OBO exchange will fail at
    # runtime if invoked, but module import should still work for tooling.
    root_agent = get_root_agent(
        session_id=session_id, actor_id=actor_id, user_jwt=os.getenv("DEV_USER_JWT", "")
    )
