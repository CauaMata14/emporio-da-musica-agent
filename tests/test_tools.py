import json

from emporio_agent import tools
from emporio_agent.tools import (
    buscar_produtos, consultar_pedido, execute_tool, listar_promocoes_ativas, max_installments,
    simular_pagamento, status_da_loja,
)


def names(result):
    return [p["nome"] for p in result["produtos"]]


# --- catalogue -------------------------------------------------------------------------------

def test_guitars_under_1000_only_available_and_within_budget():
    r = buscar_produtos(categoria="Violões", preco_max=1000, apenas_disponiveis=True, limite=25)
    assert r["total_encontrado"] == 12
    assert all(p["disponivel_para_compra"] for p in r["produtos"])
    assert all(p.get("promocao", {}).get("preco_promocional", p["preco_tabela"]) <= 1000 for p in r["produtos"])
    assert r["indisponiveis_omitidos"] == 2  # GF-3D (no stock) and SN-7C (discontinued)


def test_exact_model_search_tolerates_formatting():
    for query in ["Takamine GD20", "takamine gd-20", "TAKAMINE gd20"]:
        assert names(buscar_produtos(termo=query))[0] == "Takamine GD20 Dreadnought Natural"


def test_accent_and_plural_insensitive():
    assert buscar_produtos(termo="violao nylon eletrico")["total_encontrado"] >= 5
    assert buscar_produtos(termo="guitarras fender")["total_encontrado"] == 2


def test_out_of_stock_product_is_reported_not_hidden():
    p = buscar_produtos(termo="Giannini GF-3D")["produtos"][0]
    assert p["disponivel_para_compra"] is False
    assert p["disponibilidade"] == "esgotado no momento"


def test_unknown_product_returns_empty_with_categories():
    r = buscar_produtos(termo="flauta transversal")
    assert r["total_encontrado"] == 0
    assert "Ukuleles" in r["categorias_existentes"]


def test_only_active_promotions_are_listed():
    ids = {p["product_id"] for p in listar_promocoes_ativas()["promocoes_ativas"]}
    assert ids == {90, 94, 121, 127}


# --- payments --------------------------------------------------------------------------------

def test_installment_rules():
    assert max_installments(40) == 1        # below R$50 per installment
    assert max_installments(159.9) == 3     # 3 x 53.30
    assert max_installments(549) == 6       # 6 x 91.50 ; 7x would be < R$100
    assert max_installments(1000) == 10
    assert max_installments(5999) == 12


def test_pix_discount_not_cumulative_with_promotion():
    regular = simular_pagamento(itens=[{"product_id": 95}])  # no promo
    assert regular["total_pix"] == round(2199 * 0.95, 2)
    promo = simular_pagamento(itens=[{"product_id": 90}])    # 18% off, no extra PIX discount
    assert promo["total_pix"] == promo["total_a_vista_debito_boleto"] == round(2399 * 0.82, 2)
    assert promo["economia_pix"] == 0


def test_payment_combination_threshold():
    assert simular_pagamento(valor=2500)["permite_combinar_formas_pagamento"] is True
    assert simular_pagamento(valor=1500)["permite_combinar_formas_pagamento"] is False


# --- orders ----------------------------------------------------------------------------------

def test_order_requires_matching_contact():
    assert "erro" in consultar_pedido(1, "someone@else.com")
    assert consultar_pedido(1, "pedro.oliveira@jmail.com")["status"] == "entregue"
    assert consultar_pedido(1, "67 98432-1098")["status"] == "entregue"
    assert consultar_pedido(1, "98432-1098")["status"] == "entregue"


def test_missing_order():
    assert "Não existe" in consultar_pedido(999, "x@y.com")["erro"]


def test_return_windows_are_computed():
    # order 7 delivered (estimated) 2026-02-17; reference date 2026-03-20 -> 31 days
    r = consultar_pedido(7, "leticia.rocha@jmail.com")["prazos_politicas"]
    assert r["dias_desde_recebimento"] == 31
    assert r["dentro_prazo_arrependimento_7_dias"] is False
    assert r["dentro_garantia_legal_90_dias"] is True


def test_delayed_shipment_flag():
    r = consultar_pedido(8, "anacarol.ferreira@coldmail.com")
    assert r["status"] == "enviado" and r["entrega_atrasada"] is True


# --- store status / dispatcher ---------------------------------------------------------------

def test_store_open_on_friday_morning():
    s = status_da_loja()
    assert s["dia_da_semana"] == "sexta-feira" and s["aberta_agora"] is True


def test_store_closed_on_holiday(monkeypatch):
    monkeypatch.setenv("AGENT_REFERENCE_DATE", "2026-11-02")  # Finados (Monday)
    s = status_da_loja()
    assert s["feriado_nacional_hoje"] is True and s["aberta_agora"] is False
    assert s["proxima_abertura"].startswith("terça-feira")


def test_execute_tool_handles_bad_input():
    out, is_error = execute_tool("detalhes_produto", {"wrong": 1})
    assert is_error and "Parâmetros inválidos" in json.loads(out)["erro"]
    out, is_error = execute_tool("nao_existe", {})
    assert is_error


def test_every_schema_has_a_function():
    assert {s["name"] for s in tools.TOOL_SCHEMAS} == set(tools.TOOL_FUNCTIONS)
