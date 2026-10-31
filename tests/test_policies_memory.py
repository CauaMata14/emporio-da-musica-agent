from emporio_agent.memory import ConversationStore
from emporio_agent.policies import SECTION_SLUGS, get_policy_sections, load_sections


def test_all_ten_sections_parsed():
    sections = load_sections()
    assert set(sections) == {str(i) for i in range(1, 11)}
    assert set(SECTION_SLUGS.values()) <= set(sections)


def test_section_content():
    r = get_policy_sections(["trocas_devolucoes", "horario_funcionamento"])
    text = " ".join(s["conteudo"] for s in r["secoes"])
    assert "7 (sete) dias corridos" in text and "09:00 às 13:00" in text


def test_invalid_section_is_reported():
    r = get_policy_sections(["nao_existe"])
    assert r["secoes"] == [] and "secoes_validas" in r


def test_store_roundtrip(tmp_path):
    store = ConversationStore(tmp_path / "c.db")
    store.append_many("s1", [{"role": "user", "content": "oi"},
                             {"role": "assistant", "content": [{"type": "text", "text": "Olá!"}]}])
    store.append_many("s1", [{"role": "user", "content": "tchau"}])
    msgs = store.load("s1")
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[1]["content"][0]["text"] == "Olá!"
    assert store.load("other") == []
