"""
FATECalçados · API do programa de fidelidade
Desafio de Inovação Data System 2026 · Missão: Fidelidade

Modelo de dados alinhado ao modelo lógico fornecido (MODELO_LOGICO_DESAFIO_DATASYSTEM):
    Clientes, Compras, Resgates e Movimentacao_Pontos.

Regras do programa (iguais para todas as equipes):
    - Acúmulo:       1 ponto a cada R$ 1,00 em compras
    - Validade:      pontos expiram em 12 meses
    - Resgate:       100 pontos = R$ 5,00 de desconto
    - Identificação: cliente identificado por CPF
    - Níveis:        Bronze 0-999 (sem bônus) · Prata 1.000-4.999 (10%) · Ouro 5.000+ (20%)
"""
import math
import os
import random
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import (Column, DateTime, Float, ForeignKey, Integer, String,
                        create_engine, func)
from sqlalchemy.orm import Session, declarative_base, sessionmaker

# ---------------------------------------------------------------------------
# REGRAS DO PROGRAMA (constantes únicas: API, seed e dashboard usam os mesmos valores)
# ---------------------------------------------------------------------------
PONTOS_POR_REAL = 1              # 1 ponto a cada R$ 1,00 pago
VALIDADE_PONTOS_DIAS = 365       # pontos expiram em 12 meses
PONTOS_POR_BLOCO_RESGATE = 100   # 100 pontos...
VALOR_POR_BLOCO_RESGATE = 5.0    # ...= R$ 5,00 de desconto
LIMITE_PRATA = 1000
LIMITE_OURO = 5000
BONUS_NIVEL = {"Bronze": 0.0, "Prata": 0.10, "Ouro": 0.20}

# Brasil não tem horário de verão desde 2019: UTC-3 fixo evita depender do tzdata do servidor
FUSO_BRASIL = timezone(timedelta(hours=-3))


def agora() -> datetime:
    """Horário de Brasília (sem tzinfo), para o gráfico diário bater com o dia do lojista."""
    return datetime.now(FUSO_BRASIL).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# BANCO DE DADOS
# SQLite por padrão; para usar Postgres no Render, basta definir DATABASE_URL.
# ---------------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./fatecalcados_v2.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Cliente(Base):
    __tablename__ = "clientes"
    cpf = Column(String(11), primary_key=True, index=True)
    nome = Column(String, nullable=False)
    saldo_pontos = Column(Integer, default=0)
    nivel_atual = Column(String, default="Bronze")


class Compra(Base):
    __tablename__ = "compras"
    id_compra = Column(Integer, primary_key=True, index=True)
    cpf_cliente = Column(String(11), ForeignKey("clientes.cpf"), index=True)
    data_compra = Column(DateTime, default=agora)
    valor_total = Column(Float, nullable=False)        # valor efetivamente pago
    # Colunas extras ao modelo lógico, necessárias para medir o custo do bônus de nível:
    valor_bruto = Column(Float, nullable=False)
    desconto_nivel = Column(Float, default=0.0)


class Resgate(Base):
    __tablename__ = "resgates"
    id_resgate = Column(Integer, primary_key=True, index=True)
    data_resgate = Column(DateTime, default=agora)
    pontos_utilizados = Column(Integer, nullable=False)
    valor_desconto_gerado = Column(Float, nullable=False)
    cpf_cliente = Column(String(11), ForeignKey("clientes.cpf"), index=True)


class MovimentacaoPontos(Base):
    """Extrato de pontos: qtd > 0 é acúmulo (ligado a uma compra); qtd < 0 é resgate."""
    __tablename__ = "movimentacao_pontos"
    id_movimentacao = Column(Integer, primary_key=True, index=True)
    data_transacao = Column(DateTime, default=agora)
    qtd_pontos = Column(Integer, nullable=False)
    data_expiracao = Column(DateTime, nullable=True)   # preenchida só nos acúmulos
    cpf_cliente = Column(String(11), ForeignKey("clientes.cpf"), index=True)
    id_compra = Column(Integer, ForeignKey("compras.id_compra"), nullable=True)
    id_resgate = Column(Integer, ForeignKey("resgates.id_resgate"), nullable=True)


Base.metadata.create_all(bind=engine)

# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
app = FastAPI(title="FATECalçados API", description="Programa de fidelidade · Desafio Data System 2026")

# CORS: o dashboard é um HTML estático (Netlify) servido de outra origem.
# Por padrão fica aberto ("*"). Em produção, defina no Render a variável ALLOWED_ORIGINS
# com o endereço do dashboard, por exemplo: https://fatecalcados.netlify.app
# (vários endereços separados por vírgula).
ORIGENS_PERMITIDAS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=ORIGENS_PERMITIDAS, allow_methods=["*"], allow_headers=["*"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class CompraCreate(BaseModel):
    cpf_cliente: str
    valor_bruto: float
    pontos_a_resgatar: int = 0


class RegraDeNegocio(Exception):
    """Violação de uma regra do programa (vira HTTP 400 nas rotas)."""


# ---------------------------------------------------------------------------
# MOTOR DE REGRAS
# ---------------------------------------------------------------------------
def nivel_por_pontos(pontos: int) -> str:
    if pontos >= LIMITE_OURO:
        return "Ouro"
    if pontos >= LIMITE_PRATA:
        return "Prata"
    return "Bronze"


def limpar_cpf(cpf: str) -> str:
    """Aceita '111.222.333-44' ou '11122233344' e devolve só os 11 dígitos."""
    digitos = "".join(ch for ch in str(cpf) if ch.isdigit())
    if len(digitos) != 11:
        raise RegraDeNegocio("CPF inválido: informe 11 dígitos.")
    return digitos


def mascarar_cpf(cpf: str) -> str:
    return f"***.{cpf[3:6]}.{cpf[6:9]}-**"


def calcular_saldo(db: Session, cpf: str, momento: datetime) -> int:
    """
    Saldo válido de pontos, a partir do extrato (Movimentacao_Pontos).

    Resgates consomem primeiro os pontos mais antigos (FIFO), que também são os primeiros a
    expirar. Logo: saldo = acumulado - max(resgatado, expirado). Assim um resgate feito com
    pontos que depois venceriam não é descontado duas vezes.
    """
    def soma(*filtros):
        return db.query(func.coalesce(func.sum(MovimentacaoPontos.qtd_pontos), 0)).filter(
            MovimentacaoPontos.cpf_cliente == cpf, *filtros).scalar()

    acumulado = soma(MovimentacaoPontos.qtd_pontos > 0)
    resgatado = -soma(MovimentacaoPontos.qtd_pontos < 0)
    expirado = soma(MovimentacaoPontos.qtd_pontos > 0, MovimentacaoPontos.data_expiracao <= momento)
    return max(0, int(acumulado - max(resgatado, expirado)))


def processar_compra(db: Session, cpf: str, valor_bruto: float, pontos_a_resgatar: int,
                     momento: datetime, nome: str | None = None) -> dict:
    """Aplica as regras do programa a uma compra e grava compra, resgate e extrato de pontos."""
    if valor_bruto <= 0:
        raise RegraDeNegocio("O valor da compra deve ser maior que zero.")
    if pontos_a_resgatar < 0 or pontos_a_resgatar % PONTOS_POR_BLOCO_RESGATE != 0:
        raise RegraDeNegocio(f"O resgate deve ser em múltiplos de {PONTOS_POR_BLOCO_RESGATE} pontos.")

    cliente = db.query(Cliente).filter(Cliente.cpf == cpf).first()
    if not cliente:
        # Cadastro automático na primeira compra (o PDV só precisa enviar o CPF)
        cliente = Cliente(cpf=cpf, nome=nome or f"Cliente {cpf}", saldo_pontos=0, nivel_atual="Bronze")
        db.add(cliente)
        db.flush()

    # Nível e saldo consideram a expiração até o momento da compra
    saldo_atual = calcular_saldo(db, cpf, momento)
    nivel = nivel_por_pontos(saldo_atual)

    if pontos_a_resgatar > saldo_atual:
        raise RegraDeNegocio("O cliente não tem pontos suficientes para este resgate.")

    desconto_nivel = round(valor_bruto * BONUS_NIVEL[nivel], 2)
    desconto_pontos = (pontos_a_resgatar / PONTOS_POR_BLOCO_RESGATE) * VALOR_POR_BLOCO_RESGATE
    if desconto_pontos > valor_bruto - desconto_nivel:
        raise RegraDeNegocio("O desconto dos pontos não pode ser maior que o valor da compra.")

    valor_pago = round(valor_bruto - desconto_nivel - desconto_pontos, 2)
    # Pontos sobre o valor PAGO: protege a margem da loja
    pontos_ganhos = math.floor(valor_pago * PONTOS_POR_REAL)

    compra = Compra(cpf_cliente=cpf, data_compra=momento, valor_bruto=valor_bruto,
                    desconto_nivel=desconto_nivel, valor_total=valor_pago)
    db.add(compra)
    db.flush()

    if pontos_a_resgatar > 0:
        resgate = Resgate(data_resgate=momento, pontos_utilizados=pontos_a_resgatar,
                          valor_desconto_gerado=desconto_pontos, cpf_cliente=cpf)
        db.add(resgate)
        db.flush()
        db.add(MovimentacaoPontos(data_transacao=momento, qtd_pontos=-pontos_a_resgatar,
                                  cpf_cliente=cpf, id_resgate=resgate.id_resgate))

    if pontos_ganhos > 0:
        db.add(MovimentacaoPontos(data_transacao=momento, qtd_pontos=pontos_ganhos,
                                  data_expiracao=momento + timedelta(days=VALIDADE_PONTOS_DIAS),
                                  cpf_cliente=cpf, id_compra=compra.id_compra))
    db.flush()

    cliente.saldo_pontos = calcular_saldo(db, cpf, momento)
    cliente.nivel_atual = nivel_por_pontos(cliente.saldo_pontos)

    return {
        "mensagem": "Compra processada com sucesso!",
        "resumo": {
            "valor_bruto": valor_bruto,
            "desconto_nivel": desconto_nivel,
            "desconto_pontos": desconto_pontos,
            "valor_pago": valor_pago,
            "pontos_ganhos": pontos_ganhos,
            "pontos_resgatados": pontos_a_resgatar,
        },
        "cliente": {
            "cpf": cliente.cpf,
            "novo_saldo_pontos": cliente.saldo_pontos,
            "nivel_atual": cliente.nivel_atual,
        },
    }


# ---------------------------------------------------------------------------
# ROTAS
# ---------------------------------------------------------------------------
@app.get("/")
def raiz():
    return {"status": "ok", "servico": "FATECalçados API"}


@app.post("/compras/")
def registrar_compra(compra: CompraCreate, db: Session = Depends(get_db)):
    """Rota que um PDV chamaria ao fechar a venda de um cliente identificado por CPF."""
    try:
        resultado = processar_compra(db, limpar_cpf(compra.cpf_cliente), compra.valor_bruto,
                                     compra.pontos_a_resgatar, agora())
        db.commit()
        return resultado
    except RegraDeNegocio as erro:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(erro))


@app.post("/manutencao/expirar-pontos")
def expirar_pontos(db: Session = Depends(get_db)):
    """Reaplica a validade de 12 meses aos saldos. Pode ser agendado (cron) uma vez por dia."""
    momento = agora()
    for cliente in db.query(Cliente).all():
        cliente.saldo_pontos = calcular_saldo(db, cliente.cpf, momento)
        cliente.nivel_atual = nivel_por_pontos(cliente.saldo_pontos)
    db.commit()
    return {"mensagem": "Saldos e níveis recalculados com a validade de 12 meses."}


# --- Dashboard ---------------------------------------------------------------
@app.get("/dashboard/kpis")
def get_kpis(db: Session = Depends(get_db)):
    def soma(coluna, *filtros):
        return db.query(func.coalesce(func.sum(coluna), 0)).filter(*filtros).scalar() or 0

    total_clientes = db.query(Cliente).count()
    total_compras = db.query(Compra).count()
    faturamento = float(soma(Compra.valor_total))
    custo_resgates = float(soma(Resgate.valor_desconto_gerado))
    custo_bonus_nivel = float(soma(Compra.desconto_nivel))

    pontos_acumulados = int(soma(MovimentacaoPontos.qtd_pontos, MovimentacaoPontos.qtd_pontos > 0))
    pontos_resgatados = int(-soma(MovimentacaoPontos.qtd_pontos, MovimentacaoPontos.qtd_pontos < 0))
    pontos_em_circulacao = int(soma(Cliente.saldo_pontos))
    pontos_expirados = max(0, pontos_acumulados - pontos_resgatados - pontos_em_circulacao)

    return {
        "total_clientes": total_clientes,
        "faturamento_liquido": faturamento,
        "custo_descontos": custo_resgates + custo_bonus_nivel,   # custo total do programa
        "custo_resgates": custo_resgates,                        # só o que veio de pontos resgatados
        "custo_bonus_nivel": custo_bonus_nivel,                  # só o que veio do bônus de nível
        "ticket_medio": faturamento / total_compras if total_compras > 0 else 0.0,
        "pontos_acumulados": pontos_acumulados,
        "pontos_resgatados": pontos_resgatados,
        "pontos_expirados": pontos_expirados,
        "pontos_em_circulacao": pontos_em_circulacao,
    }


@app.get("/dashboard/grafico")
def get_grafico_temporal(db: Session = Depends(get_db)):
    """Últimos 8 dias (hoje e os 7 anteriores): faturamento e pontos acumulados vs resgatados."""
    hoje = agora()
    dias = [(hoje - timedelta(days=i)).date() for i in range(7, -1, -1)]
    inicio = datetime.combine(dias[0], datetime.min.time())

    por_dia = {d: {"data": d.strftime("%d/%m"), "valor": 0.0,
                   "pontos_acumulados": 0, "pontos_resgatados": 0} for d in dias}

    for compra in db.query(Compra).filter(Compra.data_compra >= inicio).all():
        dia = compra.data_compra.date()
        if dia in por_dia:
            por_dia[dia]["valor"] += compra.valor_total

    for mov in db.query(MovimentacaoPontos).filter(MovimentacaoPontos.data_transacao >= inicio).all():
        dia = mov.data_transacao.date()
        if dia in por_dia:
            if mov.qtd_pontos > 0:
                por_dia[dia]["pontos_acumulados"] += mov.qtd_pontos
            else:
                por_dia[dia]["pontos_resgatados"] += -mov.qtd_pontos

    return [por_dia[d] for d in dias]


@app.get("/dashboard/niveis")
def get_distribuicao_niveis(db: Session = Depends(get_db)):
    contagem = {n: 0 for n in ("Ouro", "Prata", "Bronze")}
    for nivel, qtd in db.query(Cliente.nivel_atual, func.count()).group_by(Cliente.nivel_atual).all():
        if nivel in contagem:
            contagem[nivel] = qtd
    return contagem


@app.get("/dashboard/ranking")
def get_ranking_clientes(db: Session = Depends(get_db)):
    """Top 5 clientes com mais pontos. CPF mascarado (LGPD)."""
    clientes = db.query(Cliente).order_by(Cliente.saldo_pontos.desc()).limit(5).all()
    return [{"nome": c.nome, "cpf": mascarar_cpf(c.cpf), "pontos": c.saldo_pontos, "nivel": c.nivel_atual}
            for c in clientes]


# --- Dados fictícios (seed) --------------------------------------------------
NOMES = ["Ana", "Carlos", "Beatriz", "João", "Mariana", "Pedro", "Lucas", "Julia", "Fernanda", "Rafael",
         "Camila", "Bruno", "Amanda", "Diego", "Leticia", "Rodrigo", "Patricia", "Thiago", "Natalia", "Marcelo"]


@app.post("/seed")
def popular_banco(resetar: bool = False, db: Session = Depends(get_db)):
    """
    Gera 20 clientes e 200 compras nos últimos 7 dias.
    As compras passam pelo MESMO motor de regras da rota /compras/, em ordem cronológica,
    então pontos, níveis, descontos e resgates saem coerentes com as regras do programa.
    """
    if db.query(Cliente).count() > 1 and not resetar:
        return {"mensagem": "O banco já possui dados. Use resetar=true para gerar novos."}

    if resetar:
        # Ordem importa por causa das chaves estrangeiras
        db.query(MovimentacaoPontos).delete()
        db.query(Resgate).delete()
        db.query(Compra).delete()
        db.query(Cliente).delete()
        db.commit()

    cpfs = [f"111222333{i:02d}" for i in range(len(NOMES))]
    for cpf, nome in zip(cpfs, NOMES):
        db.add(Cliente(cpf=cpf, nome=nome, saldo_pontos=0, nivel_atual="Bronze"))
    db.commit()

    # Alguns clientes compram muito mais que outros: gera Bronze, Prata e Ouro
    pesos = [random.choice([0.3, 0.6, 1, 1, 1.5, 2.5, 4]) for _ in cpfs]
    agora_ = agora()
    eventos = []
    for _ in range(200):
        momento = agora_ - timedelta(days=random.randint(0, 6), hours=random.randint(0, 23),
                                     minutes=random.randint(0, 59))
        eventos.append((momento, random.choices(cpfs, weights=pesos)[0],
                        round(random.triangular(89, 750, 260), 2)))
    eventos.sort(key=lambda e: e[0])   # pontos precisam ser acumulados em ordem cronológica

    resgates_feitos = 0
    for momento, cpf, valor in eventos:
        saldo = calcular_saldo(db, cpf, momento)
        blocos = min(saldo // PONTOS_POR_BLOCO_RESGATE, 6)
        pontos = random.randint(1, blocos) * PONTOS_POR_BLOCO_RESGATE if blocos >= 1 and random.random() < 0.3 else 0
        try:
            processar_compra(db, cpf, valor, pontos, momento)
        except RegraDeNegocio:
            processar_compra(db, cpf, valor, 0, momento)   # resgate inviável nesta compra: segue sem resgatar
            pontos = 0
        resgates_feitos += 1 if pontos else 0
    db.commit()

    return {"mensagem": f"{len(cpfs)} clientes e {len(eventos)} compras fictícias geradas "
                        f"({resgates_feitos} com resgate de pontos)."}