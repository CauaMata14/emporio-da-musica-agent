"""Tests the function-calling loop with a fake client that returns real SDK response objects."""

from google.genai import types

from emporio_agent.agent import EmporioAgent
from emporio_agent.config import Settings
from emporio_agent.memory import ConversationStore


def _resp(parts, finish_reason=types.FinishReason.STOP):
    return types.GenerateContentResponse(candidates=[types.Candidate(
        content=types.Content(role="model", parts=parts), finish_reason=finish_reason)])


def _text(text):
    return types.Part(text=text)


def _call(name, args, call_id=None):
    return types.Part(function_call=types.FunctionCall(id=call_id, name=name, args=args))


class FakeModels:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class FakeClient:
    def __init__(self, responses):
        self.models = FakeModels(responses)


def make_agent(tmp_path, responses):
    client = FakeClient(responses)
    agent = EmporioAgent(Settings(), ConversationStore(tmp_path / "t.db"), client)
    return agent, client


def test_tool_round_then_answer(tmp_path):
    agent, client = make_agent(tmp_path, [
        _resp([_call("buscar_produtos", {"termo": "Takamine GD20"}, "fc_1")]),
        _resp([_text("O Takamine GD20 sai por R$ 2.199,00.")]),
    ])
    reply = agent.chat("s1", "Quanto custa o Takamine GD20?")

    assert "2.199" in reply.text
    assert [c.name for c in reply.tool_calls] == ["buscar_produtos"]
    # second API call carries the function_response for the call id
    fn = client.models.calls[1]["contents"][-1]["parts"][0]["function_response"]
    assert fn["id"] == "fc_1" and fn["name"] == "buscar_produtos"
    assert "Takamine GD20 Dreadnought" in fn["response"]["output"]
    # whole turn persisted: user, model(call), user(response), model(text)
    assert [m["role"] for m in agent.store.load("s1")] == ["user", "model", "user", "model"]


def test_history_is_reused_across_turns(tmp_path):
    agent, client = make_agent(tmp_path, [
        _resp([_text("Oi! Sou a Lia.")]),
        _resp([_text("Claro!")]),
    ])
    agent.chat("s1", "oi")
    agent.chat("s1", "e aí?")
    assert len(client.models.calls[1]["contents"]) == 3
    assert agent.history_for_display("s1") == [("user", "oi"), ("assistant", "Oi! Sou a Lia."),
                                               ("user", "e aí?"), ("assistant", "Claro!")]


def test_thought_signature_survives_persistence(tmp_path):
    part = _call("status_da_loja", {}, "fc_1")
    part.thought_signature = b"\x00sig"
    agent, client = make_agent(tmp_path, [_resp([part]), _resp([_text("ok")]), _resp([_text("ok")])])
    agent.chat("s1", "tá aberta?")
    agent.chat("s1", "valeu")
    replayed = types.Content.model_validate(client.models.calls[2]["contents"][1])
    assert replayed.parts[0].thought_signature == b"\x00sig"


def test_blocked_answer_is_not_persisted(tmp_path):
    agent, _ = make_agent(tmp_path, [_resp([], types.FinishReason.SAFETY)])
    reply = agent.chat("s1", "...")
    assert "não consigo ajudar" in reply.text
    assert agent.store.load("s1") == []


def test_parallel_tool_calls_return_in_one_message(tmp_path):
    agent, client = make_agent(tmp_path, [
        _resp([_call("status_da_loja", {}, "a"),
               _call("consultar_politicas", {"secoes": ["sobre_a_loja"]}, "b")]),
        _resp([_text("ok")]),
    ])
    agent.chat("s1", "Onde fica a loja e está aberta?")
    parts = client.models.calls[1]["contents"][-1]["parts"]
    assert [p["function_response"]["id"] for p in parts] == ["a", "b"]
