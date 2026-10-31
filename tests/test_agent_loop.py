"""Tests the tool-use loop with a fake client that returns real SDK message objects."""

from anthropic.types import Message, TextBlock, ToolUseBlock, Usage

from emporio_agent.agent import EmporioAgent
from emporio_agent.config import Settings
from emporio_agent.memory import ConversationStore


def _msg(content, stop_reason):
    return Message(id="msg_x", type="message", role="assistant", model="fake", content=content,
                   stop_reason=stop_reason, stop_sequence=None,
                   usage=Usage(input_tokens=1, output_tokens=1))


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class FakeClient:
    def __init__(self, responses):
        self.messages = FakeMessages(responses)
        self.beta = self  # beta.messages.create -> same fake


def make_agent(tmp_path, responses):
    client = FakeClient(responses)
    agent = EmporioAgent(Settings(enable_fallback=False), ConversationStore(tmp_path / "t.db"), client)
    return agent, client


def test_tool_round_then_answer(tmp_path):
    agent, client = make_agent(tmp_path, [
        _msg([ToolUseBlock(id="tu_1", type="tool_use", name="buscar_produtos",
                           input={"termo": "Takamine GD20"})], "tool_use"),
        _msg([TextBlock(type="text", text="O Takamine GD20 sai por R$ 2.199,00.")], "end_turn"),
    ])
    reply = agent.chat("s1", "Quanto custa o Takamine GD20?")

    assert "2.199" in reply.text
    assert [c.name for c in reply.tool_calls] == ["buscar_produtos"]
    # second API call carries the tool_result for the tool_use id
    second = client.messages.calls[1]["messages"]
    assert second[-1]["content"][0]["type"] == "tool_result"
    assert second[-1]["content"][0]["tool_use_id"] == "tu_1"
    assert "Takamine GD20 Dreadnought" in second[-1]["content"][0]["content"]
    # whole turn persisted: user, assistant(tool_use), user(tool_result), assistant(text)
    assert [m["role"] for m in agent.store.load("s1")] == ["user", "assistant", "user", "assistant"]


def test_history_is_reused_across_turns(tmp_path):
    agent, client = make_agent(tmp_path, [
        _msg([TextBlock(type="text", text="Oi! Sou a Lia.")], "end_turn"),
        _msg([TextBlock(type="text", text="Claro!")], "end_turn"),
    ])
    agent.chat("s1", "oi")
    agent.chat("s1", "e aí?")
    assert len(client.messages.calls[1]["messages"]) == 3
    assert agent.history_for_display("s1")[0] == ("user", "oi")


def test_refusal_is_not_persisted(tmp_path):
    agent, _ = make_agent(tmp_path, [_msg([], "refusal")])
    reply = agent.chat("s1", "...")
    assert "não consigo ajudar" in reply.text
    assert agent.store.load("s1") == []


def test_parallel_tool_calls_return_in_one_message(tmp_path):
    agent, client = make_agent(tmp_path, [
        _msg([ToolUseBlock(id="a", type="tool_use", name="status_da_loja", input={}),
              ToolUseBlock(id="b", type="tool_use", name="consultar_politicas",
                           input={"secoes": ["sobre_a_loja"]})], "tool_use"),
        _msg([TextBlock(type="text", text="ok")], "end_turn"),
    ])
    agent.chat("s1", "Onde fica a loja e está aberta?")
    results = client.messages.calls[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["a", "b"]
