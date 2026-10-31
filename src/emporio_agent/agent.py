"""The agent: a hand-written function-calling loop over the Gemini API (google-genai SDK).

Why a manual loop (and not LangChain/LlamaIndex or the SDK's automatic function calling):
- The whole agent is ~150 lines that are easy to read, test and explain.
- Full control over what is persisted, how errors/blocked answers are handled and how many tool
  rounds are allowed; no framework abstractions between the prompt and the model.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
from google import genai
from google.genai import errors, types

from .config import Settings
from .memory import ConversationStore
from .prompts import SYSTEM_PROMPT
from .tools import TOOL_SCHEMAS, execute_tool

log = logging.getLogger(__name__)

MSG_TECH_ERROR = ("Ops, tive um probleminha técnico agora 😕 Pode tentar de novo em instantes? "
                  "Se preferir, fale com a equipe pelo (67) 3341-4444.")
MSG_REFUSAL = ("Desculpe, não consigo ajudar com isso por aqui. Posso te ajudar com instrumentos, "
               "pedidos ou informações da loja?")

# Finish reasons that mean "the model declined / the answer was blocked".
BLOCKED_REASONS = {
    types.FinishReason.SAFETY, types.FinishReason.BLOCKLIST, types.FinishReason.PROHIBITED_CONTENT,
    types.FinishReason.SPII, types.FinishReason.RECITATION,
}

TOOLS = [types.Tool(function_declarations=[
    types.FunctionDeclaration(name=s["name"], description=s["description"],
                              parameters_json_schema=s["input_schema"])
    for s in TOOL_SCHEMAS
])]


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


def _content_to_dict(content: types.Content) -> dict[str, Any]:
    """SDK Content -> plain dict that can be stored as JSON and sent back unchanged
    (thought signatures included, base64-encoded)."""
    return content.model_dump(mode="json", exclude_none=True)


class EmporioAgent:
    def __init__(
        self,
        settings: Settings | None = None,
        store: ConversationStore | None = None,
        client: Any | None = None,
    ):
        self.settings = settings or Settings()
        self.store = store or ConversationStore(self.settings.db_path)
        # Retries 429/5xx with exponential backoff (the free tier has low per-minute limits and
        # occasional 503 "high demand"); the timeout (ms) keeps a stuck request from hanging the chat.
        self.client = client or genai.Client(http_options=types.HttpOptions(
            timeout=60_000, retry_options=types.HttpRetryOptions(attempts=3, initial_delay=2.0)))
        self.config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=TOOLS,
            # We run the tools ourselves (see chat()), so the SDK must not.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            thinking_config=types.ThinkingConfig(thinking_level=self.settings.thinking_level),
            max_output_tokens=self.settings.max_tokens,
        )

    # -- API call ----------------------------------------------------------------------------
    def _create(self, contents: list[dict[str, Any]]) -> types.GenerateContentResponse:
        return self.client.models.generate_content(
            model=self.settings.model, contents=contents, config=self.config)

    # -- public ------------------------------------------------------------------------------
    def chat(self, session_id: str, user_text: str) -> AgentReply:
        history = self.store.load(session_id)
        new_messages: list[dict[str, Any]] = [{"role": "user", "parts": [{"text": user_text}]}]
        tool_calls: list[ToolCall] = []

        for _ in range(self.settings.max_tool_rounds):
            try:
                response = self._create(history + new_messages)
            except errors.APIError as exc:  # 4xx/5xx after the SDK's retries
                log.error("API error %s: %s", exc.code, exc.message)
                return AgentReply(MSG_TECH_ERROR, tool_calls, error=True)
            except httpx.HTTPError as exc:
                log.error("Connection error: %s", exc)
                return AgentReply(MSG_TECH_ERROR, tool_calls, error=True)

            candidate = response.candidates[0] if response.candidates else None
            if candidate is None or candidate.finish_reason in BLOCKED_REASONS or not candidate.content:
                # Not persisted: keeps the stored history clean for the next turn.
                log.warning("Blocked/empty answer: %s", candidate.finish_reason if candidate else
                            response.prompt_feedback)
                return AgentReply(MSG_REFUSAL, tool_calls)

            parts = candidate.content.parts or []
            new_messages.append(_content_to_dict(candidate.content))
            calls = [p.function_call for p in parts if p.function_call]

            if not calls:
                text = "".join(p.text for p in parts if p.text and not p.thought).strip()
                if candidate.finish_reason == types.FinishReason.MAX_TOKENS:
                    text += "\n\n(…resposta cortada, pode me pedir para continuar.)"
                self.store.append_many(session_id, new_messages)
                return AgentReply(text, tool_calls)

            # Run every tool requested in this round; return all results in ONE user message.
            results = []
            for call in calls:
                args = dict(call.args or {})
                output, is_error = execute_tool(call.name, args)
                tool_calls.append(ToolCall(call.name, args, output, is_error))
                log.info("tool %s(%s) -> error=%s", call.name, json.dumps(args, ensure_ascii=False), is_error)
                fn_response: dict[str, Any] = {"name": call.name,
                                               "response": {"error" if is_error else "output": output}}
                if call.id:
                    fn_response["id"] = call.id
                results.append({"function_response": fn_response})
            new_messages.append({"role": "user", "parts": results})

        log.warning("Max tool rounds reached for session %s", session_id)
        return AgentReply(MSG_TECH_ERROR, tool_calls, error=True)

    def history_for_display(self, session_id: str) -> list[tuple[str, str]]:
        """(role, text) pairs with only the human-readable parts of the conversation."""
        pairs = []
        for msg in self.store.load(session_id):
            text = "".join(p.get("text", "") for p in msg["parts"] if not p.get("thought")).strip()
            if text:
                pairs.append(("assistant" if msg["role"] == "model" else "user", text))
        return pairs
