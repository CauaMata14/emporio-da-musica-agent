"""Tools exposed to the LLM (function calling).

Design principles:
- The LLM decides *when* to call a tool; Python does every lookup, filter and calculation.
  Prices, discounts, installments and date windows are computed here, never "by the model",
  because the policy manual says prices/stock/deadlines must never be given without checking
  the system (policy 7.1) and LLMs are unreliable at arithmetic.
- Every tool returns plain JSON-serializable dicts with Portuguese keys (the agent answers in
  Portuguese) and explicit flags (e.g. `disponivel`, `dentro_do_prazo_arrependimento`) so the
  model applies the rules instead of re-deriving them.
- Order lookups require the order number AND the customer's e-mail or phone (LGPD, policy 9).
"""

from __future__ import annotations

import json
import secrets
from datetime import date, datetime, time, timedelta
from typing import Any, Callable

import pandas as pd

from . import config
from .data import get_store_data, normalize_text, only_digits
from .policies import SECTION_SLUGS, get_policy_sections

# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------

STOPWORDS = {
    "de", "da", "do", "das", "dos", "para", "pra", "com", "sem", "um", "uma", "o", "a", "os", "as",
    "e", "ou", "em", "no", "na", "ate", "por", "que", "tem", "voces", "quero", "algum", "alguma",
}
_SUFFIXES = ("oes", "aes", "ao", "as", "os", "es", "a", "o", "e", "s")

STATUS_LABELS = {
    "active": "ativo",
    "discontinued": "descontinuado",
    "coming_soon": "em breve (pré-lançamento)",
}

ORDER_STATUS = {
    "pending": ("pendente", "Pedido recebido, aguardando confirmação do pagamento."),
    "confirmed": ("confirmado", "Pagamento confirmado; pedido em separação, ainda não despachado."),
    "shipped": ("enviado", "Pedido despachado; pode ser acompanhado pelo código de rastreio."),
    "delivered": ("entregue", "Pedido entregue."),
    "cancelled": ("cancelado", "Pedido cancelado."),
}

PAYMENT_LABELS = {"pix": "PIX", "debit": "cartão de débito", "boleto": "boleto bancário"}

WEEKDAYS_PT = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira",
               "sábado", "domingo"]

# Opening hours from policy section 2 (weekday() -> (open, close)); Sunday closed.
OPENING_HOURS = {i: (time(9), time(18)) for i in range(5)} | {5: (time(9), time(13))}

# Fixed-date national holidays (store is closed on holidays — policy 2). Movable holidays
# (Good Friday, Corpus Christi) and municipal ones are not modelled: see README limitations.
NATIONAL_HOLIDAYS = {(1, 1), (4, 21), (5, 1), (9, 7), (10, 12), (11, 2), (11, 15), (11, 20), (12, 25)}


def _stem(word: str) -> str:
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _tokens(text: str) -> list[str]:
    return [_stem(t) for t in normalize_text(text).split() if t not in STOPWORDS]


def _money(value: float) -> float:
    return round(float(value) + 1e-9, 2)


def _is_holiday(d: date) -> bool:
    return (d.month, d.day) in NATIONAL_HOLIDAYS


def _effective_price(row: pd.Series) -> float:
    return float(row["promo_price"]) if pd.notna(row["promo_discount"]) else float(row["price_brl"])


def _product_dict(row: pd.Series, detailed: bool = False) -> dict[str, Any]:
    has_promo = pd.notna(row["promo_discount"])
    status = row["status"]
    in_stock = int(row["stock_quantity"]) > 0
    available = status == "active" and in_stock
    if status != "active":
        availability = STATUS_LABELS.get(status, status)
    else:
        availability = "em estoque" if in_stock else "esgotado no momento"

    item: dict[str, Any] = {
        "product_id": int(row["product_id"]),
        "nome": row["name"],
        "categoria": row["category"],
        "preco_tabela": _money(row["price_brl"]),
        "disponivel_para_compra": bool(available),
        "disponibilidade": availability,
        "unidades_em_estoque": int(row["stock_quantity"]) if status == "active" else 0,
    }
    if has_promo:
        item["promocao"] = {
            "nome": row["promo_name"],
            "desconto_percentual": int(row["promo_discount"]),
            "preco_promocional": _money(row["promo_price"]),
            "observacao": "Desconto PIX não é cumulativo com a promoção (política 6.2).",
        }
    else:
        item["preco_pix_5_off"] = _money(row["price_brl"] * 0.95)
    if detailed:
        item["descricao"] = row["description"]
        item["especificacoes"] = row["specs"]
    return item


# ---------------------------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------------------------


def buscar_produtos(
    termo: str | None = None,
    categoria: str | None = None,
    preco_min: float | None = None,
    preco_max: float | None = None,
    apenas_disponiveis: bool = False,
    ordenar_por: str = "relevancia",
    limite: int = 10,
) -> dict[str, Any]:
    df = get_store_data().products.copy()
    df["preco_efetivo"] = df.apply(_effective_price, axis=1)

    if categoria:
        cat_norm = _stem(normalize_text(categoria).split()[0]) if normalize_text(categoria) else ""
        df = df[df["category"].map(lambda c: cat_norm in normalize_text(c))]
    if preco_min is not None:
        df = df[df["preco_efetivo"] >= float(preco_min)]
    if preco_max is not None:
        df = df[df["preco_efetivo"] <= float(preco_max)]

    partial = False
    if termo and termo.strip():
        query_tokens = _tokens(termo)
        compact_query = normalize_text(termo).replace(" ", "")

        def score(row: pd.Series) -> float:
            words = {_stem(w) for w in row["search_text"].split()}
            name_words = {_stem(w) for w in row["name_norm"].split()}
            s = sum(2.0 if t in name_words else 1.0 for t in query_tokens if t in words)
            if compact_query and compact_query in row["name_norm"].replace(" ", ""):
                s += 3.0  # e.g. "gd-20" -> "gd20" matches "Takamine GD20"
            return s

        df = df.assign(_score=df.apply(score, axis=1) if len(df) else [])
        full = df[df.apply(lambda r: all(t in {_stem(w) for w in r["search_text"].split()}
                                         for t in query_tokens), axis=1)] if len(df) else df
        if len(full):
            df = full
        else:
            df = df[df["_score"] > 0]
            partial = len(df) > 0
        if ordenar_por == "relevancia":
            df = df.sort_values(["_score", "preco_efetivo"], ascending=[False, True])

    unavailable_hidden = 0
    if apenas_disponiveis:
        mask = (df["status"] == "active") & (df["stock_quantity"] > 0)
        unavailable_hidden = int((~mask).sum())
        df = df[mask]

    if ordenar_por == "preco_asc":
        df = df.sort_values("preco_efetivo")
    elif ordenar_por == "preco_desc":
        df = df.sort_values("preco_efetivo", ascending=False)

    limite = max(1, min(int(limite or 10), 25))
    result: dict[str, Any] = {
        "total_encontrado": int(len(df)),
        "produtos": [_product_dict(r) for _, r in df.head(limite).iterrows()],
    }
    if partial:
        result["aviso"] = "Nenhum produto corresponde a todos os termos; exibindo correspondências parciais."
    if unavailable_hidden:
        result["indisponiveis_omitidos"] = unavailable_hidden
    if len(df) > limite:
        result["aviso_limite"] = f"Exibindo {limite} de {len(df)}. Refine a busca se necessário."
    if not len(df):
        result["categorias_existentes"] = get_store_data().categories["name"].tolist()
    return result


def detalhes_produto(product_id: int) -> dict[str, Any]:
    df = get_store_data().products
    row = df[df["product_id"] == int(product_id)]
    if row.empty:
        return {"erro": f"Produto {product_id} não encontrado no catálogo."}
    return _product_dict(row.iloc[0], detailed=True)


def listar_promocoes_ativas() -> dict[str, Any]:
    df = get_store_data().products
    promos = df[df["promo_discount"].notna()].sort_values("promo_discount", ascending=False)
    return {
        "promocoes_ativas": [_product_dict(r) for _, r in promos.iterrows()],
        "observacao": "Somente promoções com is_active=1 no sistema. Promoções não são cumulativas.",
    }


def max_installments(total: float) -> int:
    """Largest number of interest-free installments allowed by policy 3.1."""
    best = 1
    for n in range(2, 13):
        value = total / n
        if (n <= 3 and value >= 50) or (4 <= n <= 6 and value >= 80) or (7 <= n <= 12 and value >= 100):
            best = n
    return best


def simular_pagamento(
    itens: list[dict[str, Any]] | None = None, valor: float | None = None
) -> dict[str, Any]:
    """Payment options for a set of catalogue items, or for a raw amount (no promotion)."""
    lines, total_list, total_effective, total_pix = [], 0.0, 0.0, 0.0
    if itens:
        df = get_store_data().products
        for it in itens:
            pid, qty = int(it["product_id"]), max(1, int(it.get("quantidade", 1)))
            row = df[df["product_id"] == pid]
            if row.empty:
                return {"erro": f"Produto {pid} não encontrado."}
            r = row.iloc[0]
            has_promo = pd.notna(r["promo_discount"])
            unit_eff = _effective_price(r)
            unit_pix = unit_eff if has_promo else float(r["price_brl"]) * 0.95
            total_list += float(r["price_brl"]) * qty
            total_effective += unit_eff * qty
            total_pix += unit_pix * qty
            lines.append({"product_id": pid, "nome": r["name"], "quantidade": qty,
                          "preco_unitario": _money(unit_eff),
                          "em_promocao": bool(has_promo)})
    elif valor is not None:
        total_list = total_effective = float(valor)
        total_pix = float(valor) * 0.95
    else:
        return {"erro": "Informe 'itens' (product_id/quantidade) ou 'valor'."}

    n_max = max_installments(total_effective)
    options = [{"parcelas": n, "valor_parcela": _money(total_effective / n)}
               for n in sorted({1, 2, 3, 6, 10, 12, n_max}) if n <= n_max]
    return {
        "itens": lines,
        "total_preco_tabela": _money(total_list),
        "total_a_vista_debito_boleto": _money(total_effective),
        "total_pix": _money(total_pix),
        "economia_pix": _money(total_effective - total_pix),
        "cartao_credito": {
            "max_parcelas_sem_juros": n_max,
            "opcoes": options,
            "regra": "Até 3x: parcela mín. R$50; 4x-6x: mín. R$80; 7x-12x: mín. R$100 (política 3.1).",
        },
        "permite_combinar_formas_pagamento": total_effective > 2000,
        "observacao": "PIX dá 5% sobre o preço de tabela e não se aplica a itens em promoção.",
    }


def _find_customer_order(numero_pedido: int, email_ou_telefone: str):
    data = get_store_data()
    order = data.orders[data.orders["order_id"] == int(numero_pedido)]
    if order.empty:
        return None, None, "pedido_nao_encontrado"
    order = order.iloc[0]
    customer = data.customers[data.customers["customer_id"] == int(order["customer_id"])].iloc[0]
    given = (email_ou_telefone or "").strip().lower()
    digits = only_digits(given)
    email_ok = "@" in given and given == customer["email_norm"]
    # accept phone with or without DDD / country code
    phone_ok = len(digits) >= 8 and customer["phone_digits"].endswith(digits[-11:] if len(digits) >= 11 else digits)
    if not (email_ok or phone_ok):
        return None, None, "dados_nao_conferem"
    return order, customer, None


def consultar_pedido(numero_pedido: int, email_ou_telefone: str) -> dict[str, Any]:
    order, customer, err = _find_customer_order(numero_pedido, email_ou_telefone)
    if err == "pedido_nao_encontrado":
        return {"erro": f"Não existe pedido com o número {numero_pedido}."}
    if err == "dados_nao_conferem":
        # Do not reveal whether the order exists for someone else (LGPD).
        return {"erro": "Não foi possível validar o pedido: o e-mail/telefone informado não "
                        "confere com o cadastro. Peça para o cliente conferir os dados."}

    data = get_store_data()
    today = config.today()
    items = data.order_items[data.order_items["order_id"] == int(order["order_id"])]
    products = data.products.set_index("product_id")
    item_list = [{"product_id": int(i.product_id), "nome": products.loc[i.product_id, "name"],
                  "quantidade": int(i.quantity)} for i in items.itertuples()]

    status = order["status"]
    label, meaning = ORDER_STATUS.get(status, (status, ""))
    payment = order["payment_method"]
    if payment.startswith("credit_"):
        payment_label = f"cartão de crédito em {payment.split('_')[1]}"
    else:
        payment_label = PAYMENT_LABELS.get(payment, payment)

    result: dict[str, Any] = {
        "numero_pedido": int(order["order_id"]),
        "cliente_primeiro_nome": customer["name"].split()[0],
        "cidade_entrega": customer["city"],
        "data_pedido": order["order_date"].isoformat(),
        "status": label,
        "significado_status": meaning,
        "itens": item_list,
        "valor_total": _money(order["total_brl"]),
        "forma_pagamento": payment_label,
        "data_de_referencia_hoje": today.isoformat(),
    }
    if pd.notna(order["tracking_code"]):
        result["codigo_rastreio"] = order["tracking_code"]
    eta = order["estimated_delivery"]
    if isinstance(eta, date):
        result["previsao_entrega"] = eta.isoformat()
        if status == "shipped" and eta < today:
            result["entrega_atrasada"] = True
            result["dias_de_atraso"] = (today - eta).days
    if pd.notna(order["notes"]):
        result["observacoes"] = order["notes"]

    # Deadlines for returns/exchanges/warranty, computed deterministically (policies 4 and 8).
    if status == "delivered" and isinstance(eta, date):
        received = eta  # assumption: no actual delivery date in the data -> use estimated_delivery
        days_since_receipt = (today - received).days
        days_since_purchase = (today - order["order_date"]).days
        result["prazos_politicas"] = {
            "data_recebimento_considerada": received.isoformat(),
            "nota": "Data de recebimento estimada (o sistema não registra a data real de entrega).",
            "dias_desde_recebimento": days_since_receipt,
            "dias_desde_compra": days_since_purchase,
            "dentro_prazo_arrependimento_7_dias": days_since_receipt <= 7,
            "dentro_prazo_troca_por_preferencia_7_dias": days_since_receipt <= 7,
            "dentro_prazo_troca_por_defeito_30_dias": days_since_purchase <= 30,
            "dentro_garantia_legal_90_dias": days_since_receipt <= 90,
        }
    elif status in {"pending", "confirmed"}:
        result["nota_atendimento"] = ("Pedido ainda não despachado. Cancelamentos/alterações não estão "
                                      "previstos no manual: encaminhe para um atendente humano.")
    return result


def status_da_loja() -> dict[str, Any]:
    """Current date/time in Campo Grande and whether the store is open now."""
    now = config.now()
    today = now.date()

    def hours_for(d: date):
        return None if _is_holiday(d) else OPENING_HOURS.get(d.weekday())

    hours = hours_for(today)
    is_open = bool(hours and hours[0] <= now.time() < hours[1])
    next_open = None
    for offset in range(0, 8):
        d = today + timedelta(days=offset)
        h = hours_for(d)
        if h and datetime.combine(d, h[0], tzinfo=now.tzinfo) > now:
            next_open = f"{WEEKDAYS_PT[d.weekday()]} ({d.strftime('%d/%m')}) às {h[0].strftime('%H:%M')}"
            break
    return {
        "agora": now.strftime("%Y-%m-%d %H:%M"),
        "dia_da_semana": WEEKDAYS_PT[today.weekday()],
        "feriado_nacional_hoje": _is_holiday(today),
        "aberta_agora": is_open,
        "horario_hoje": f"{hours[0]:%H:%M} às {hours[1]:%H:%M}" if hours else "fechada",
        "proxima_abertura": None if is_open else next_open,
    }


def encaminhar_para_atendente(
    motivo: str, resumo: str, nome_cliente: str | None = None,
    contato: str | None = None, numero_pedido: int | None = None,
) -> dict[str, Any]:
    """Register a hand-off ticket for the human team (complaints, cancellations, quotes...)."""
    protocol = f"EM-{config.today():%Y%m%d}-{secrets.token_hex(2).upper()}"
    ticket = {"protocolo": protocol, "criado_em": config.now().isoformat(), "motivo": motivo,
              "resumo": resumo, "nome_cliente": nome_cliente, "contato": contato,
              "numero_pedido": numero_pedido}
    config.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    with open(config.RUNTIME_DIR / "handoffs.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(ticket, ensure_ascii=False) + "\n")
    return {"protocolo": protocol, "prazo_retorno": "até 24 horas úteis",
            "registrado": True}


def consultar_politicas(secoes: list[str]) -> dict[str, Any]:
    return get_policy_sections(secoes)


# ---------------------------------------------------------------------------------------------
# schemas exposed to Claude
# ---------------------------------------------------------------------------------------------

CATEGORY_NAMES = [
    "Guitarras", "Baixos", "Baterias e Percussão", "Teclados e Pianos", "Violões",
    "Instrumentos de Sopro (Madeiras)", "Instrumentos de Sopro (Metais)", "Cordas Orquestrais",
    "Ukuleles",
]

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "buscar_produtos",
        "description": (
            "Busca instrumentos no catálogo com preço, promoção ativa e disponibilidade em tempo real. "
            "Use para qualquer pergunta sobre produtos, preços, modelos ou estoque — nunca responda "
            "preço/estoque sem consultar. Filtros são opcionais e combináveis. Ao listar OPÇÕES ou "
            "recomendações, use apenas_disponiveis=true; ao procurar um modelo específico, deixe false "
            "para saber se ele está esgotado/descontinuado e sugerir alternativas."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "termo": {"type": "string", "description": "Palavras-chave: marca, modelo, tipo (ex.: 'Takamine GD20', 'nylon elétrico', 'Yamaha')."},
                "categoria": {"type": "string", "enum": CATEGORY_NAMES},
                "preco_min": {"type": "number", "description": "Preço mínimo em R$ (considera preço promocional, se houver)."},
                "preco_max": {"type": "number", "description": "Preço máximo em R$ (considera preço promocional, se houver)."},
                "apenas_disponiveis": {"type": "boolean", "description": "Se true, omite esgotados, descontinuados e pré-lançamentos."},
                "ordenar_por": {"type": "string", "enum": ["relevancia", "preco_asc", "preco_desc"]},
                "limite": {"type": "integer", "minimum": 1, "maximum": 25},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "detalhes_produto",
        "description": "Retorna descrição completa, especificações técnicas, preço e disponibilidade de um produto pelo product_id (obtido via buscar_produtos).",
        "input_schema": {
            "type": "object",
            "properties": {"product_id": {"type": "integer"}},
            "required": ["product_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "listar_promocoes_ativas",
        "description": "Lista os produtos com promoção ATIVA no momento. Use quando o cliente perguntar por promoções/ofertas ou citar uma promoção específica (ex.: Black Friday) para verificar se ainda está vigente.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "simular_pagamento",
        "description": (
            "Calcula valores por forma de pagamento: total no PIX (5% off, não cumulativo com promoção), "
            "à vista no débito/boleto, número máximo de parcelas sem juros e valor das parcelas, e se é "
            "possível combinar formas de pagamento. Informe 'itens' (preferível) ou um 'valor' avulso."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "itens": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "product_id": {"type": "integer"},
                            "quantidade": {"type": "integer", "minimum": 1},
                        },
                        "required": ["product_id"],
                        "additionalProperties": False,
                    },
                },
                "valor": {"type": "number", "description": "Valor em R$, quando não houver produtos específicos."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "consultar_pedido",
        "description": (
            "Consulta status, itens, rastreio, previsão de entrega e os prazos de devolução/troca/garantia "
            "já calculados de um pedido. Exige o número do pedido E o e-mail ou telefone do cadastro do "
            "cliente (LGPD) — peça os dois antes de chamar."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "numero_pedido": {"type": "integer"},
                "email_ou_telefone": {"type": "string", "description": "E-mail ou telefone informado pelo cliente."},
            },
            "required": ["numero_pedido", "email_ou_telefone"],
            "additionalProperties": False,
        },
    },
    {
        "name": "consultar_politicas",
        "description": (
            "Lê seções do manual oficial de políticas da loja. Use antes de responder sobre endereço/contato "
            "(sobre_a_loja), horários, pagamento/parcelamento, trocas/devoluções/arrependimento, frete/entrega/"
            "rastreio, regras de promoção, escopo do atendimento e reclamações (atendimento), garantia ou "
            "privacidade. Pode pedir mais de uma seção."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "secoes": {"type": "array", "items": {"type": "string", "enum": list(SECTION_SLUGS)}, "minItems": 1},
            },
            "required": ["secoes"],
            "additionalProperties": False,
        },
    },
    {
        "name": "status_da_loja",
        "description": "Data e hora atuais em Campo Grande/MS e se a loja está aberta agora (considera domingo e feriados nacionais). Use para 'vocês estão abertos?', 'abre amanhã?' e para saber a data de hoje.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "encaminhar_para_atendente",
        "description": (
            "Registra um chamado para a equipe humana e gera um protocolo. Use para reclamações, pedidos de "
            "cancelamento/alteração, solicitações de troca/devolução que precisem ser formalizadas, cotação de "
            "frete especial, ou quando o cliente pedir para falar com uma pessoa. Confirme o resumo com o cliente."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "motivo": {"type": "string", "enum": ["reclamacao", "cancelamento_ou_alteracao", "troca_ou_devolucao",
                                                      "garantia", "cotacao_frete", "falar_com_atendente", "outro"]},
                "resumo": {"type": "string", "description": "Resumo objetivo da solicitação para a equipe."},
                "nome_cliente": {"type": "string"},
                "contato": {"type": "string", "description": "Telefone ou e-mail para retorno."},
                "numero_pedido": {"type": "integer"},
            },
            "required": ["motivo", "resumo"],
            "additionalProperties": False,
        },
    },
]

TOOL_FUNCTIONS: dict[str, Callable[..., dict[str, Any]]] = {
    "buscar_produtos": buscar_produtos,
    "detalhes_produto": detalhes_produto,
    "listar_promocoes_ativas": listar_promocoes_ativas,
    "simular_pagamento": simular_pagamento,
    "consultar_pedido": consultar_pedido,
    "consultar_politicas": consultar_politicas,
    "status_da_loja": status_da_loja,
    "encaminhar_para_atendente": encaminhar_para_atendente,
}


def execute_tool(name: str, tool_input: dict[str, Any]) -> tuple[str, bool]:
    """Run a tool and return (json_result, is_error)."""
    func = TOOL_FUNCTIONS.get(name)
    if func is None:
        return json.dumps({"erro": f"Ferramenta desconhecida: {name}"}), True
    try:
        result = func(**(tool_input or {}))
    except (TypeError, ValueError, KeyError) as exc:
        return json.dumps({"erro": f"Parâmetros inválidos para {name}: {exc}"}, ensure_ascii=False), True
    return json.dumps(result, ensure_ascii=False, default=str), "erro" in result
