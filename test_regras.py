"""
Validação das regras do programa de fidelidade FATECalçados.

Como rodar (na pasta do main.py):
    pip install fastapi sqlalchemy httpx uvicorn
    python test_regras.py

Usa um banco temporário (não mexe no fatecalcados_v2.db).
"""
import os
import tempfile
from datetime import timedelta

_dir = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_dir}/teste.db"

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from main import (Cliente, Compra, MovimentacaoPontos, Resgate, SessionLocal,  # noqa: E402
                  agora, calcular_saldo, nivel_por_pontos, processar_compra)

client = TestClient(main.app)
ok = 0


def confere(descricao, condicao):
    global ok
    assert condicao, f"FALHOU: {descricao}"
    ok += 1
    print(f"  ok  {descricao}")


print("Seed")
resp = client.post("/seed?resetar=true")
confere("seed responde 200", resp.status_code == 200)
db = SessionLocal()
confere("pelo menos 20 clientes", db.query(Cliente).count() >= 20)
confere("pelo menos 200 compras", db.query(Compra).count() >= 200)
confere("existem resgates no seed", db.query(Resgate).count() > 0)

print("Coerência do dashboard com as regras")
k = client.get("/dashboard/kpis").json()
confere("acumulados - resgatados - expirados = pontos em circulação",
        k["pontos_acumulados"] - k["pontos_resgatados"] - k["pontos_expirados"] == k["pontos_em_circulacao"])
confere("custo dos resgates = pontos resgatados / 100 x R$ 5,00",
        abs(k["custo_resgates"] - k["pontos_resgatados"] / 100 * 5) < 0.01)
confere("custo do programa = resgates + bônus de nível",
        abs(k["custo_descontos"] - (k["custo_resgates"] + k["custo_bonus_nivel"])) < 0.01)
confere("ticket médio = faturamento / nº de compras",
        abs(k["ticket_medio"] - k["faturamento_liquido"] / db.query(Compra).count()) < 0.01)
niveis = client.get("/dashboard/niveis").json()
confere("distribuição por nível soma o total de clientes", sum(niveis.values()) == k["total_clientes"])
grafico = client.get("/dashboard/grafico").json()
confere("gráfico cobre 8 dias", len(grafico) == 8)
confere("gráfico soma o faturamento total", abs(sum(d["valor"] for d in grafico) - k["faturamento_liquido"]) < 0.01)
confere("gráfico soma os pontos acumulados e resgatados",
        sum(d["pontos_acumulados"] for d in grafico) == k["pontos_acumulados"]
        and sum(d["pontos_resgatados"] for d in grafico) == k["pontos_resgatados"])

print("Cada cliente respeita níveis e saldo")
for c in db.query(Cliente).all():
    assert c.nivel_atual == nivel_por_pontos(c.saldo_pontos), c.cpf
    assert c.saldo_pontos == calcular_saldo(db, c.cpf, agora()), c.cpf
confere("nível compatível com o saldo (Bronze <1.000, Prata <5.000, Ouro >=5.000)", True)
confere("saldo armazenado = saldo recalculado pelo extrato", True)
for compra in db.query(Compra).all():
    assert compra.valor_total >= 0
    assert compra.valor_total <= compra.valor_bruto
confere("nenhuma compra com valor final negativo ou maior que o bruto", True)

print("Regras na rota de compra (PDV)")
r = client.post("/compras/", json={"cpf_cliente": "987.654.321-00", "valor_bruto": 250.0}).json()
confere("cliente novo é cadastrado por CPF e ganha 1 ponto por R$ 1,00 pago",
        r["resumo"]["pontos_ganhos"] == 250 and r["cliente"]["nivel_atual"] == "Bronze")
r = client.post("/compras/", json={"cpf_cliente": "98765432100", "valor_bruto": 100.0, "pontos_a_resgatar": 200}).json()
confere("200 pontos viram R$ 10,00 de desconto", r["resumo"]["desconto_pontos"] == 10.0 and r["resumo"]["valor_pago"] == 90.0)
confere("saldo desconta o resgate e soma os novos pontos", r["cliente"]["novo_saldo_pontos"] == 250 - 200 + 90)
confere("resgate acima do saldo é recusado",
        client.post("/compras/", json={"cpf_cliente": "98765432100", "valor_bruto": 100.0, "pontos_a_resgatar": 1000}).status_code == 400)
confere("resgate fora de múltiplos de 100 é recusado",
        client.post("/compras/", json={"cpf_cliente": "98765432100", "valor_bruto": 100.0, "pontos_a_resgatar": 50}).status_code == 400)
confere("CPF inválido é recusado",
        client.post("/compras/", json={"cpf_cliente": "123", "valor_bruto": 100.0}).status_code == 400)

print("Bônus de nível")
processar_compra(db, "11111111111", 1000.0, 0, agora(), "Cliente Prata")   # 1.000 pts -> Prata
db.commit()
r = processar_compra(db, "11111111111", 500.0, 0, agora())
confere("Prata recebe bônus de 10%", r["resumo"]["desconto_nivel"] == 50.0)
processar_compra(db, "22222222222", 5000.0, 0, agora(), "Cliente Ouro")    # 5.000 pts -> Ouro
db.commit()
r = processar_compra(db, "22222222222", 500.0, 0, agora())
confere("Ouro recebe bônus de 20%", r["resumo"]["desconto_nivel"] == 100.0)

print("Validade de 12 meses")
processar_compra(db, "33333333333", 1200.0, 0, agora() - timedelta(days=400), "Cliente Antigo")
db.commit()
confere("pontos de 400 dias atrás já expiraram", calcular_saldo(db, "33333333333", agora()) == 0)
processar_compra(db, "44444444444", 1200.0, 0, agora() - timedelta(days=100), "Cliente Recente")
db.commit()
confere("pontos de 100 dias atrás continuam válidos", calcular_saldo(db, "44444444444", agora()) == 1200)
client.post("/manutencao/expirar-pontos")
db.expire_all()
confere("manutenção zera o saldo vencido e rebaixa o nível",
        db.get(Cliente, "33333333333").saldo_pontos == 0 and db.get(Cliente, "33333333333").nivel_atual == "Bronze")
db.close()

print(f"\n{ok} verificações passaram.")