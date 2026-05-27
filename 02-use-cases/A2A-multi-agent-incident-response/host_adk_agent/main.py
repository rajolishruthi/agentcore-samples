import logging
from dotenv import load_dotenv
from google.adk.sessions import InMemorySessionService
from google.adk.runners import Runner
from google.genai import types
from bedrock_agentcore import BedrockAgentCoreApp

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables from .env file
load_dotenv()

APP_NAME = "HostAgentA2A"
#runtime calls when a request comes in 
app = BedrockAgentCoreApp() 

session_service = InMemorySessionService()

# Per-(session, user) cache so memory and resolved agent cards persist across
# turns, but identity-bound state (OBO tokens) tracks the actual user.
_agent_cache: dict[tuple[str, str], object] = {}


@app.entrypoint
async def call_agent(payload: dict, context):
    session_id = context.session_id
    logger.info(f"Received request with session_id: {session_id}")

    actor_id = context.request_headers[
        "x-amzn-bedrock-agentcore-runtime-custom-actorid"
    ]

    if not actor_id:
        raise Exception("Actor id is not is not set")

    if not session_id:
        raise Exception("Context session_id is not set")

    # Extract the user JWT (validated upstream by AgentCore Runtime authorizer)
    # so we can exchange it via the OBO Pre-Token-Gen flow for downstream calls.
    auth_header = context.request_headers.get("authorization") or context.request_headers.get("Authorization") or ""
    if not auth_header.lower().startswith("bearer "):
        raise Exception("Missing Bearer token in Authorization header")
    user_jwt = auth_header[7:]

    cache_key = (session_id, actor_id)
    root_agent = _agent_cache.get(cache_key)
    if root_agent is None:
        # Import agent creation inside entrypoint so workload identity is available
        from agent import get_agent_and_card

        logger.info("Initializing root agent and resolving agent cards...")
        try:
            root_agent, agents_cards = await get_agent_and_card(
                session_id=session_id, actor_id=actor_id, user_jwt=user_jwt
            )
            _agent_cache[cache_key] = root_agent
            logger.info(
                f"Successfully initialized root agent. Agent cards: {list(agents_cards.keys())}"
            )
        except Exception as e:
            logger.error(f"Failed to initialize root agent: {e}", exc_info=True)
            raise

        yield agents_cards

    query = payload.get("prompt")
    logger.info(f"Processing query: {query}")

    if not query:
        raise KeyError("'prompt' field is required in payload")

    in_memory_session = session_service.get_session_sync(
        app_name=APP_NAME, user_id=actor_id, session_id=session_id
    )

    if not in_memory_session:
        # Session doesn't exist, create it
        _ = session_service.create_session_sync(
            app_name=APP_NAME, user_id=actor_id, session_id=session_id
        )

    runner = Runner(
        agent=root_agent, app_name=APP_NAME, session_service=session_service
    )

    content = types.Content(role="user", parts=[types.Part(text=query)])

    # Use async run to properly maintain event loop across invocations
    async for event in runner.run_async(
        user_id=actor_id, session_id=session_id, new_message=content
    ):
        yield event


if __name__ == "__main__":
    app.run()  # Ready to run on Bedrock AgentCore
