"""The agent: a hand-written tool-use loop over the Claude Messages API.

Why a manual loop (and not LangChain/LlamaIndex or the SDK's beta tool runner):
- The whole agent is ~150 lines that are easy to read, test and explain.
- Full control over what is persisted, how errors/refusals are handled and how many tool
  rounds are allowed; no framework abstractions between the prompt and the model.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import anthropic

from .config import Settings
from .memory import ConversationStore
from .prompts import SYSTEM_PROMPT
from .tools import TOOL_SCHEMAS, execute_tool

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"

MSG_TECH_ERROR = ("Ops, tive um probleminha técnico agora 😕 Pode tentar de novo em instantes? "
                  "Se preferir, fale com a equipe pelo (67) 3341-4444.")
MSG_REFUSAL = ("Desculpe, não consigo ajudar com isso por aqui. Posso te ajudar com instrumentos, "
               "pedidos ou informações da loja?")


@dataclass
class ToolCall:
    name: str
    input: dict[str, Any]
    output: str
    is_error: bool


@dataclass
class AgentReply:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    error: bool = False


def _block_to_param(block: Any) -> dict[str, Any]:
    """SDK content block -> plain dict that can be sent back to the API and stored as JSON."""
    return block.model_dump(mode="json", exclude_none=True)


class EmporioAgent:
    def __init__(
        self,
        settings: Settings | None = None,
        store: ConversationStore | None = None,
        client: Any | None = None,
    ):
        self.settings = settings or Settings()
        self.store = store or ConversationStore(self.settings.db_path)
        self.client = client or anthropic.Anthropic()

    # -- API call ----------------------------------------------------------------------------
    def _create(self, messages: list[dict[str, Any]]):
        params: dict[str, Any] = dict(
            model=self.settings.model,
            max_tokens=self.settings.max_tokens,
            system=SYSTEM_PROMPT,
            tools=TOOL_SCHEMAS,
            messages=messages,
            output_config={"effort": self.settings.effort},
            # Automatic prompt caching: tools + system prompt + previous turns are reused.
            cache_control={"type": "ephemeral"},
        )
        if self.settings.enable_fallback:
            # If a safety classifier declines, the API re-runs the request on a fallback model.
            return self.client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **params)
        return self.client.messages.create(**params)

    # -- public ------------------------------------------------------------------------------
    def chat(self, session_id: str, user_text: str) -> AgentReply:
        history = self.store.load(session_id)
        new_messages: list[dict[str, Any]] = [{"role": "user", "content": user_text}]
        tool_calls: list[ToolCall] = []

        for _ in range(self.settings.max_tool_rounds):
            try:
                response = self._create(history + new_messages)
            # The SDK already retries 429/5xx/connection errors (max_retries=2) before raising.
            except anthropic.APIConnectionError as exc:
                log.error("Connection error: %s", exc)
                return AgentReply(MSG_TECH_ERROR, tool_calls, error=True)
            except anthropic.RateLimitError as exc:
                log.error("Rate limited: %s", exc)
                return AgentReply(MSG_TECH_ERROR, tool_calls, error=True)
            except anthropic.APIStatusError as exc:
                log.error("API error %s: %s", exc.status_code, exc.message)
                return AgentReply(MSG_TECH_ERROR, tool_calls, error=True)

            if response.stop_reason == "refusal":
                # Not persisted: keeps the stored history clean for the next turn.
                return AgentReply(MSG_REFUSAL, tool_calls)

            new_messages.append({"role": "assistant",
                                 "content": [_block_to_param(b) for b in response.content]})

            if response.stop_reason != "tool_use":
                text = "".join(b.text for b in response.content if b.type == "text").strip()
                if response.stop_reason == "max_tokens":
                    text += "\n\n(…resposta cortada, pode me pedir para continuar.)"
                self.store.append_many(session_id, new_messages)
                return AgentReply(text, tool_calls)

            # Run every tool requested in this round; return all results in ONE user message.
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                output, is_error = execute_tool(block.name, block.input)
                tool_calls.append(ToolCall(block.name, block.input, output, is_error))
                log.info("tool %s(%s) -> error=%s", block.name, json.dumps(block.input, ensure_ascii=False), is_error)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": output, "is_error": is_error})
            new_messages.append({"role": "user", "content": results})

        log.warning("Max tool rounds reached for session %s", session_id)
        return AgentReply(MSG_TECH_ERROR, tool_calls, error=True)

    def history_for_display(self, session_id: str) -> list[tuple[str, str]]:
        """(role, text) pairs with only the human-readable parts of the conversation."""
        pairs = []
        for msg in self.store.load(session_id):
            content = msg["content"]
            if isinstance(content, str):
                pairs.append((msg["role"], content))
                continue
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
            if text:
                pairs.append((msg["role"], text))
        return pairs
