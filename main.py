from fastapi import FastAPI, HTTPException, Depends
from sqlalchemy import create_engine, Column, String, Integer, Float, DateTime, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from pydantic import BaseModel
from datetime import datetime
import math

# NOVOS IMPORTS PARA A FASE 3
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func
from datetime import timedelta
import random

# --- FASE 1: CONFIGURAÇÃO DA BASE DE DADOS ---
SQLALCHEMY_DATABASE_URL = "sqlite:///./fatecalcados.db" 
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
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
    cpf_cliente = Column(String(11), ForeignKey("clientes.cpf")) 
    data_compra = Column(DateTime, default=datetime.now)
    valor_bruto = Column(Float, nullable=False)
    desconto_nivel = Column(Float, default=0.0)
    desconto_pontos = Column(Float, default=0.0)
    valor_final = Column(Float, nullable=False)

Base.metadata.create_all(bind=engine)

# --- FASE 2: INICIALIZAÇÃO DA API (FastAPI) ---
app = FastAPI(title="FATECalçados API")

# FASE 3: Configuração do CORS (Permite que o Dashboard HTML no navegador converse com a API)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Permitir de qualquer lugar (útil para testes locais)
    allow_methods=["*"],
    allow_headers=["*"],
)

# Dependência para abrir e fechar a conexão com o banco a cada requisição
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# SCHEMA PYDANTIC: Define o formato de dados que a API espera receber do Frontend
class CompraCreate(BaseModel):
    cpf_cliente: str
    valor_bruto: float
    pontos_a_resgatar: int = 0

# --- ROTA CORE: MOTOR DE NEGÓCIOS ---
@app.post("/compras/")
def registrar_compra(compra: CompraCreate, db: Session = Depends(get_db)):
    # 1. Buscar o cliente no banco de dados
    cliente = db.query(Cliente).filter(Cliente.cpf == compra.cpf_cliente).first()
    
    # Se o cliente não existir, vamos criá-lo automaticamente para facilitar os testes
    if not cliente:
        cliente = Cliente(cpf=compra.cpf_cliente, nome=f"Cliente {compra.cpf_cliente}")
        db.add(cliente)
        db.commit() # Salva o cliente novo
        db.refresh(cliente)

    # 2. Calcular Desconto do Nível (Baseado no seu documento)
    tx_desconto = 0.0
    if cliente.nivel_atual == "Ouro":
        tx_desconto = 0.20
    elif cliente.nivel_atual == "Prata":
        tx_desconto = 0.10
        
    desconto_nivel = compra.valor_bruto * tx_desconto

    # 3. Calcular Desconto de Pontos (100 pts = R$ 5)
    if compra.pontos_a_resgatar > cliente.saldo_pontos:
        raise HTTPException(status_code=400, detail="O cliente não tem pontos suficientes para este resgate.")
    
    desconto_pontos = (compra.pontos_a_resgatar / 100) * 5.0

    # 4. A Matemática Final Estratégica
    valor_final = compra.valor_bruto - desconto_nivel - desconto_pontos
    
    if valor_final < 0:
        valor_final = 0.0 # Proteção: a loja nunca fica devendo dinheiro ao cliente!

    # 5. Atualização de Pontos (Estratégia: Pontos ganhos sobre o valor PAGO para proteger a margem)
    pontos_ganhos = math.floor(valor_final)
    cliente.saldo_pontos = (cliente.saldo_pontos - compra.pontos_a_resgatar) + pontos_ganhos

    # 6. Atualização de Nível
    if cliente.saldo_pontos >= 5000:
        cliente.nivel_atual = "Ouro"
    elif cliente.saldo_pontos >= 1000:
        cliente.nivel_atual = "Prata"
    # Bronze já é o padrão caso caia abaixo de 1000.

    # 7. Salvar o Histórico da Compra no Banco
    nova_compra = Compra(
        cpf_cliente=cliente.cpf,
        valor_bruto=compra.valor_bruto,
        desconto_nivel=desconto_nivel,
        desconto_pontos=desconto_pontos,
        valor_final=valor_final
    )
    db.add(nova_compra)
    db.commit()

    return {
        "mensagem": "Compra processada com sucesso!",
        "resumo": {
            "valor_bruto": compra.valor_bruto,
            "desconto_nivel": desconto_nivel,
            "desconto_pontos": desconto_pontos,
            "valor_pago": valor_final
        },
        "cliente": {
            "cpf": cliente.cpf,
            "novo_saldo_pontos": cliente.saldo_pontos,
            "nivel_atual": cliente.nivel_atual
        }
    }

# --- FASE 3: ENDPOINTS DO DASHBOARD ---

@app.get("/dashboard/kpis")
def get_kpis(db: Session = Depends(get_db)):
    total_clientes = db.query(Cliente).count()
    faturamento = db.query(func.sum(Compra.valor_final)).scalar() or 0.0
    custo_descontos = db.query(func.sum(Compra.desconto_nivel + Compra.desconto_pontos)).scalar() or 0.0
    total_compras = db.query(Compra).count()
    
    ticket_medio = faturamento / total_compras if total_compras > 0 else 0.0

    return {
        "total_clientes": total_clientes,
        "faturamento_liquido": faturamento,
        "custo_descontos": custo_descontos,
        "ticket_medio": ticket_medio
    }

@app.get("/dashboard/grafico")
def get_grafico_temporal(db: Session = Depends(get_db)):
    # Simulação de faturamento dos últimos 7 dias para o gráfico
    hoje = datetime.now()
    dados = []
    for i in range(7, -1, -1):
        data_alvo = hoje - timedelta(days=i)
        compras = db.query(Compra).all()
        # Filtra e soma as compras do dia específico
        faturamento_dia = sum(c.valor_final for c in compras if c.data_compra.date() == data_alvo.date())
        dados.append({"data": data_alvo.strftime("%d/%m"), "valor": faturamento_dia})
    return dados

@app.get("/dashboard/niveis")
def get_distribuicao_niveis(db: Session = Depends(get_db)):
    ouro = db.query(Cliente).filter(Cliente.nivel_atual == "Ouro").count()
    prata = db.query(Cliente).filter(Cliente.nivel_atual == "Prata").count()
    bronze = db.query(Cliente).filter(Cliente.nivel_atual == "Bronze").count()
    return {"Ouro": ouro, "Prata": prata, "Bronze": bronze}

@app.get("/dashboard/ranking")
def get_ranking_clientes(db: Session = Depends(get_db)):
    # Retorna os top 5 clientes com mais pontos
    clientes = db.query(Cliente).order_by(Cliente.saldo_pontos.desc()).limit(5).all()
    return [{"nome": c.nome, "pontos": c.saldo_pontos, "nivel": c.nivel_atual} for c in clientes]

# --- ROTA PARA DADOS FICTÍCIOS (Pessoa 4 do Desafio) ---
@app.post("/seed")
def popular_banco(db: Session = Depends(get_db)):
    if db.query(Cliente).count() > 1: # Verifica se já geramos dados antes
         return {"mensagem": "O banco já possui dados."}
         
    # Criando 20 clientes
    nomes = ["Ana", "Carlos", "Beatriz", "João", "Mariana", "Pedro", "Lucas", "Julia", "Fernanda", "Rafael", "Camila", "Bruno", "Amanda", "Diego", "Leticia", "Rodrigo", "Patricia", "Thiago", "Natalia", "Marcelo"]
    clientes_criados = []
    for i, nome in enumerate(nomes):
        # Simulando que alguns já têm pontos para vermos níveis diferentes
        pontos = random.randint(0, 6000)
        nivel = "Bronze"
        if pontos >= 5000: nivel = "Ouro"
        elif pontos >= 1000: nivel = "Prata"
        
        c = Cliente(cpf=f"111222333{i:02d}", nome=nome, saldo_pontos=pontos, nivel_atual=nivel)
        db.add(c)
        clientes_criados.append(c)
    db.commit()

    # Criando 200 compras distribuídas nos últimos 7 dias
    for _ in range(200):
        cliente = random.choice(clientes_criados)
        dias_atras = random.randint(0, 7)
        data_compra = datetime.now() - timedelta(days=dias_atras)
        
        valor_bruto = round(random.uniform(100, 1500), 2)
        
        # Cálculo simplificado de descontos para o seed
        tx_desconto = 0.20 if cliente.nivel_atual == "Ouro" else (0.10 if cliente.nivel_atual == "Prata" else 0.0)
        desconto_nivel = valor_bruto * tx_desconto
        valor_final = valor_bruto - desconto_nivel
        
        compra = Compra(
            cpf_cliente=cliente.cpf,
            data_compra=data_compra,
            valor_bruto=valor_bruto,
            desconto_nivel=desconto_nivel,
            valor_final=valor_final
        )
        db.add(compra)
    db.commit()
    return {"mensagem": "20 Clientes e 200 compras fictícias geradas com sucesso!"}