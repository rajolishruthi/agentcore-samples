"""A2A Agent Executor for the Strands-based web search agent.

This replaces the OpenAI-specific executor. The streaming logic is simpler
because Strands yields plain text chunks instead of OpenAI event objects.
"""

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import (
    InternalError,
    InvalidParamsError,
    Part,
    TaskState,
    TextPart,
)
from a2a.utils import new_agent_text_message, new_task
from a2a.utils.errors import ServerError
from agent import WebSearchAgent
import os
import logging

logger = logging.getLogger(__name__)


class WebSearchAgentExecutor(AgentExecutor):
    """Agent executor wrapping the Strands web search agent for A2A."""

    def __init__(self):
        self._agent = None
        self._active_tasks = {}
        logger.info("WebSearchAgentExecutor (Strands) initialized")

    async def _get_agent(self, session_id: str, actor_id: str):
        """Lazily initialize the Strands agent."""
        if self._agent is None:
            logger.info("Creating Strands web search agent...")
            memory_id = os.getenv("MEMORY_ID")
            model_id = os.getenv(
                "MODEL_ID", "global.anthropic.claude-sonnet-4-20250514-v1:0"
            )
            region_name = os.getenv("MCP_REGION")

            if not memory_id or not region_name:
                raise RuntimeError(
                    "Missing required env vars: MEMORY_ID or MCP_REGION"
                )

            self._agent = WebSearchAgent(
                memory_id=memory_id,
                model_id=model_id,
                region_name=region_name,
                actor_id=actor_id,
                session_id=session_id,
            )
            logger.info("Strands web search agent created successfully")
        return self._agent

    async def _execute_streaming(
        self, agent, user_message: str, updater: TaskUpdater,
        task_id: str, session_id: str,
    ) -> None:
        """Execute agent with streaming and update task status."""
        accumulated_text = ""
        try:
            async for event in agent.stream(user_message, session_id):
                if not self._active_tasks.get(task_id, False):
                    logger.info(f"Task {task_id} was cancelled")
                    return

                if "error" in event:
                    raise Exception(event.get("content", "Unknown error"))

                content = event.get("content", "")
                if content and not event.get("is_task_complete", False):
                    accumulated_text += content
                    await updater.update_status(
                        TaskState.working,
                        new_agent_text_message(
                            accumulated_text,
                            updater.context_id,
                            updater.task_id,
                        ),
                    )

            if accumulated_text:
                await updater.add_artifact(
                    [Part(root=TextPart(text=accumulated_text))],
                    name="agent_response",
                )
            await updater.complete()

        except Exception as e:
            logger.error(f"Error in streaming: {e}", exc_info=True)
            raise

    async def execute(
        self, context: RequestContext, event_queue: EventQueue,
    ) -> None:
        """Execute the agent for a given A2A request."""
        session_id = None
        actor_id = None

        if context.call_context:
            headers = context.call_context.state.get("headers", {})
            # On GCP Cloud Run, these come as regular HTTP headers from the caller
            session_id = headers.get(
                "x-amzn-bedrock-agentcore-runtime-session-id"
            )
            actor_id = headers.get(
                "x-amzn-bedrock-agentcore-runtime-custom-actorid"
            )

        if not actor_id:
            logger.error("Actor ID is not set")
            raise ServerError(error=InvalidParamsError())
        if not session_id:
            logger.error("Session ID is not set")
            raise ServerError(error=InvalidParamsError())

        task = context.current_task
        if not task:
            task = new_task(context.message)
            await event_queue.enqueue_event(task)

        updater = TaskUpdater(event_queue, task.id, task.context_id)
        task_id = context.task_id

        try:
            logger.info(f"Executing task {task.id}")
            user_message = context.get_user_input()
            if not user_message:
                raise ServerError(error=InvalidParamsError())

            agent = await self._get_agent(session_id, actor_id)
            self._active_tasks[task_id] = True

            await self._execute_streaming(
                agent, user_message, updater, task_id, session_id
            )
            logger.info(f"Task {task_id} completed successfully")

        except ServerError:
            raise
        except Exception as e:
            logger.error(f"Error executing task {task_id}: {e}", exc_info=True)
            raise ServerError(error=InternalError()) from e
        finally:
            self._active_tasks.pop(task_id, None)

    async def cancel(
        self, context: RequestContext, event_queue: EventQueue,
    ) -> None:
        """Cancel an ongoing task."""
        task_id = context.task_id
        logger.info(f"Cancelling task {task_id}")
        try:
            self._active_tasks[task_id] = False
            task = context.current_task
            if task:
                updater = TaskUpdater(event_queue, task.id, task.context_id)
                await updater.cancel()
        except Exception as e:
            logger.error(f"Error cancelling task {task_id}: {e}", exc_info=True)
            raise ServerError(error=InternalError()) from e
