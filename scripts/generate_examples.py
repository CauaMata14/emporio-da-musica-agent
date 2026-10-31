"""Run the scripted scenarios against the real agent and save the transcripts to examples/.

    python scripts/generate_examples.py            # all scenarios
    python scripts/generate_examples.py 3          # only scenario 3

The reference date is frozen at 2026-02-20 (a Friday, 10:00 in Campo Grande) because the
dataset's orders are from late 2025 / early 2026 — with the real current date every order would
be far outside every return window, which would make the policy scenarios uninteresting.
The transcripts are written exactly as the agent answered; nothing is edited by hand.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("AGENT_REFERENCE_DATE", "2026-02-20")

from emporio_agent.agent import EmporioAgent  # noqa: E402
from emporio_agent.config import Settings  # noqa: E402
from emporio_agent.memory import ConversationStore  # noqa: E402

SCENARIOS = [
    ("01_catalogo_violoes", "Product catalogue search with budget and follow-up comparison", [
        "Oi! Quais opções de violões vocês têm custando até R$1000?",
        "Sou iniciante e queria corda de nylon. Qual você indica? Quanto fica no PIX?",
        "E o Giannini GF-3D, vocês têm?",
    ]),
    ("02_preco_parcelamento_promocao", "Price lookup, installment rules and expired vs. active promotions", [
        "Quanto custa o Takamine GD20?",
        "Dá pra parcelar em quantas vezes sem juros?",
        "Vi que teve promoção de Black Friday no Takamine, ainda tá valendo? Tem alguma promoção agora?",
    ]),
    ("03_devolucao_arrependimento", "Return request: identity check, order data + return policy, hand-off", [
        "Me arrependi da minha compra, posso devolver meu pedido?",
        "É o pedido 7, meu e-mail é leticia.rocha@jmail.com",
        "Não usei, tá na caixa ainda. Quero devolver sim, pode abrir o pedido de devolução.",
    ]),
    ("04_info_loja_e_fora_de_escopo", "General store info, accessories (out of catalogue) and off-topic requests", [
        "Qual o endereço da loja? Vocês abrem no sábado?",
        "Vocês vendem cordas e amplificador?",
        "Beleza. Aproveitando, me ajuda a fazer um resumo sobre a Revolução Francesa pra escola?",
    ]),
    ("05_pedido_defeito_garantia", "Order status with wrong then right contact, defect outside 30 days -> warranty", [
        "Oi, quero saber do meu pedido 6",
        "gabriel@gmail.com",
        "Ah, desculpa, é gabriel.araujo@jmail.com",
        "O violão chegou, mas o captador tá dando um chiado. Consigo trocar?",
    ]),
]


def run(selected: set[int] | None = None) -> None:
    out_dir = ROOT / "examples"
    out_dir.mkdir(exist_ok=True)
    settings = Settings()
    # Fresh, throw-away history DB so every scenario starts from a clean conversation.
    store = ConversationStore(":memory:")
    agent = EmporioAgent(settings, store)

    for idx, (slug, title, turns) in enumerate(SCENARIOS, start=1):
        if selected and idx not in selected:
            continue
        session = f"example-{slug}"
        store.clear(session)
        lines = [
            f"# Example {idx} — {title}",
            "",
            f"- Model: `{settings.model}` (effort `{settings.effort}`)",
            f"- Reference date: {os.environ['AGENT_REFERENCE_DATE']} 10:00 (America/Campo_Grande)",
            f"- Generated: {datetime.now():%Y-%m-%d %H:%M} by `scripts/generate_examples.py` (unedited)",
            "",
            "---",
        ]
        print(f"\n=== {slug}")
        for user in turns:
            reply = agent.chat(session, user)
            print(f"> {user}\n{reply.text}\n")
            lines += ["", f"**Cliente:** {user}", ""]
            if reply.tool_calls:
                lines += ["<details><summary>🔧 Tools called: "
                          + ", ".join(f"<code>{c.name}</code>" for c in reply.tool_calls)
                          + "</summary>", ""]
                for c in reply.tool_calls:
                    lines += [f"- `{c.name}({json.dumps(c.input, ensure_ascii=False)})`"
                              + (" → error" if c.is_error else "")]
                lines += ["", "</details>", ""]
            lines += ["**Lia:**", "", reply.text]
        (out_dir / f"{slug}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"saved examples/{slug}.md")


if __name__ == "__main__":
    run({int(a) for a in sys.argv[1:]} or None)
