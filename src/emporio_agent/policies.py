"""Access to the store-policy manual, section by section.

Retrieval strategy: the manual has 10 well-delimited sections (~2.5k words in total), so instead
of embeddings + vector search the agent picks the section(s) it needs from a closed list
(an enum in the tool schema). This is deterministic, has no retrieval misses caused by chunking,
and costs no extra infrastructure. See README > "Retrieval".
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from .config import DATA_PROCESSED

POLICIES_MD = DATA_PROCESSED / "policies.md"

# slug exposed to the LLM -> section number in the manual
SECTION_SLUGS: dict[str, str] = {
    "sobre_a_loja": "1",          # about, mission, company data (address, phone, e-mail, CNPJ)
    "horario_funcionamento": "2",
    "formas_pagamento": "3",      # PIX discount, cards, boleto, installment rules
    "trocas_devolucoes": "4",     # right of withdrawal, defects, preference, non-eligible items
    "frete_entregas": "5",        # local delivery, other cities, tracking codes
    "promocoes": "6",
    "atendimento": "7",           # tone, scope (no accessories), special situations, complaints
    "garantia": "8",
    "privacidade_lgpd": "9",
    "disposicoes_finais": "10",
}


@lru_cache(maxsize=1)
def load_sections(path: Path = POLICIES_MD) -> dict[str, dict[str, str]]:
    """Parse policies.md into {section_number: {"title": ..., "text": ...}}."""
    text = path.read_text(encoding="utf-8")
    sections: dict[str, dict[str, str]] = {}
    for match in re.finditer(r"^## (\d+)\. (.+?)\n(.*?)(?=^## |\Z)", text, flags=re.S | re.M):
        number, title, body = match.groups()
        sections[number] = {"title": title.strip(), "text": body.strip()}
    return sections


def get_policy_sections(slugs: list[str]) -> dict:
    sections = load_sections()
    found, unknown = [], []
    for slug in dict.fromkeys(slugs):  # dedupe, keep order
        number = SECTION_SLUGS.get(slug)
        if number is None or number not in sections:
            unknown.append(slug)
            continue
        sec = sections[number]
        found.append({"secao": f"{number}. {sec['title']}", "conteudo": sec["text"]})
    result: dict = {"secoes": found}
    if unknown:
        result["secoes_invalidas"] = unknown
        result["secoes_validas"] = list(SECTION_SLUGS)
    return result
