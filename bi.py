"""Painel de BI das cotações finalizadas (lê finalizadas.csv; não altera o arquivo).

Rodar:  streamlit run bi.py
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

ARQUIVO = Path(__file__).with_name("finalizadas.csv")

# Paleta (cores validadas para daltonismo): série única em azul; até 3 categorias nas dispersões.
AZUL = "#2a78d6"
CATEGORICAS = ["#2a78d6", "#eb6834", "#1baf7a"]
CINZA_OUTROS = "#898781"
ESCALA_SEQUENCIAL = ["#cde2fb", "#86b6ef", "#3987e5", "#256abf", "#184f95", "#0d366b"]

DIMENSOES = {
    "Fornecedor": "fornecedor",
    "Marca": "marca_bi",
    "Produto e tipo": "produto_tipo",
    "Produto": "produto",
    "Especificação": "especificacao",
    "Descrição completa": "descricao",
    "Órgão": "orgao",
    "Ano": "ano",
    "Mês": "mes",
}
NIVEIS_ITEM = {  # rótulo -> coluna, do mais geral ao mais detalhado
    "Produto": "produto",
    "Produto e tipo": "produto_tipo",
    "Especificação": "especificacao",
    "Descrição completa": "descricao",
}
AJUDA_NIVEIS = ("**Produto**: só o nome (SABAO). **Produto e tipo**: SABAO (PO) ≠ SABAO (BARRA). "
                "**Especificação**: produto e tipo + capacidade + cor + embalagem (SABAO (PO) · PACOTE 500 GRAMAS), "
                "o nível certo para comparar preço. **Descrição completa**: texto exato do catálogo.")


# ---------------------------------------------------------------- dados

def normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return " ".join(texto.upper().split())


CAPACIDADE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:LITROS?|L)\b")
COR = re.compile(r"\b(PRET[AO]|AZUL|VERDE|VERMELH[AO]|AMAREL[AO]|BRANC[AO]|CINZA|MARROM|LARANJA|LEITOSA|TRANSPARENTE)\b")
COR_MASCULINO = {"PRETA": "PRETO", "BRANCA": "BRANCO", "VERMELHA": "VERMELHO", "AMARELA": "AMARELO"}
# 2º trecho da descrição que NÃO é um tipo de produto (é medida, cor, embalagem...)
NAO_E_TIPO = re.compile(r"^(CAPACIDADE|COR|EMBALAGEM|TEOR|VISCOSIDADE|PRINCIPIO|ACAO|DIMENS|MEDIDA|TAMANHO|COMPOSICAO)\b")


def produto_e_tipo(descricao: str) -> str:
    """'SABAO, PO, TENSOATIVO...' -> 'SABAO (PO)'; 'VASSOURA, NYLON, ...' -> 'VASSOURA (NYLON)'."""
    trechos = [t.strip() for t in descricao.split(",")]
    nome = trechos[0]
    tipo = trechos[1] if len(trechos) > 1 else ""
    if tipo and len(tipo.split()) <= 3 and not NAO_E_TIPO.match(tipo) and not COR.fullmatch(tipo):
        return f"{nome} ({tipo})"
    return nome


def especificacao(produto_tipo: str, descricao: str, unidade: str) -> str:
    """Produto e tipo + capacidade + cor + embalagem: o que precisa ser igual para comparar preço.

    Ex.: "SACO PARA LIXO (PLASTICO) · 100 L · PRETO · PACOTE 100 UN" (≠ "... · PACOTE 10 UN").
    """
    partes = [produto_tipo]
    unidade = re.sub(r"(\d)\.0\b", r"\1", unidade.upper())
    unidade = re.sub(r"^UNIDADE\s+(?=\d)", "", unidade)          # "UNIDADE 1.0 UNIDADE" -> "1 UNIDADE"
    unidade = re.sub(r"\bUNIDADES?\b", "UN", unidade)
    if (m := CAPACIDADE.search(descricao)) and "LITRO" not in unidade:
        partes.append(f"{m.group(1).replace(',', '.')} L")
    if m := COR.search(descricao):
        partes.append(COR_MASCULINO.get(m.group(1), m.group(1)))
    if unidade:
        partes.append(unidade)
    return " · ".join(partes)


def numero(serie: pd.Series) -> pd.Series:
    """'1.234,5600' -> 1234.56"""
    return pd.to_numeric(serie.str.replace(".", "", regex=False).str.replace(",", ".", regex=False), errors="coerce")


@st.cache_data(show_spinner="Carregando dados...")
def carregar(caminho: str, _modificado_em: float) -> pd.DataFrame:
    df = pd.read_csv(caminho, sep=";", encoding="utf-8-sig", dtype=str, keep_default_na=False)
    for col in ["total_estimado_licitacao", "total_lance_vencedor", "quantidade",
                "valor_estimado", "total_estimado", "valor_lance", "total_lance"]:
        df[col] = numero(df[col])
    df["abertura"] = pd.to_datetime(df["abertura_propostas"], format="%d/%m/%Y %H:%M", errors="coerce")
    df["acolhimento"] = pd.to_datetime(df["inicio_acolhimento"], format="%d/%m/%Y %H:%M", errors="coerce")
    df["ano"] = df["abertura"].dt.year.astype("Int64").astype(str)
    df["mes"] = df["abertura"].dt.strftime("%Y-%m")
    # "20260007 - CENTRO DE EDUCAÇÃO..." -> "CENTRO DE EDUCAÇÃO..." (o número é do edital, não do órgão)
    df["orgao"] = df["contratante"].str.replace(r"^\d+\s*-\s*", "", regex=True).str.strip(" -")
    df["edital"] = df["contratante"].str.extract(r"^(\d+)", expand=False).fillna("")
    df["produto"] = df["descricao"].str.split(",").str[0].str.strip()
    df["produto_tipo"] = df["descricao"].map(produto_e_tipo)
    df["especificacao"] = [especificacao(p, d, u) for p, d, u
                           in zip(df["produto_tipo"], df["descricao"], df["unidade_fornecimento"])]
    # "Absolut", "ABSOLUT" e "absolut" são a mesma marca
    df["marca_bi"] = df["marca"].map(normalizar).replace("", "(sem marca)")
    df["vencido"] = df["fornecedor_item"] != ""
    df["fornecedor"] = df["fornecedor_item"].replace("", "(sem vencedor)")
    df["desconto"] = (1 - df["valor_lance"] / df["valor_estimado"]).where(df["vencido"] & (df["valor_estimado"] > 0))
    # Busca na descrição e na embalagem ("5 litros" costuma estar só em "BOMBONA 5.0 LITROS")
    df["busca"] = (df["descricao"] + " " + df["especificacao"]).map(normalizar).str.replace(r"(\d)\.0\b", r"\1", regex=True)
    return df


# ---------------------------------------------------------------- formatação e componentes

def brl(valor: float) -> str:
    return "R$ " + f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def inteiro(valor: float) -> str:
    return f"{valor:,.0f}".replace(",", ".")


def pct(valor: float) -> str:
    return f"{valor:.1%}".replace(".", ",")


def curto(texto: str, n: int = 55) -> str:
    return texto if len(texto) <= n else texto[: n - 1] + "…"


def mais_frequente(serie: pd.Series) -> str:
    s = serie[serie != ""]
    return s.value_counts().index[0] if len(s) else ""


def preco_mais_frequente(serie: pd.Series) -> float:
    s = serie.dropna().round(2)
    return s.mode().iloc[0] if len(s) else float("nan")


def vezes_no_preco_mais_frequente(serie: pd.Series) -> int:
    s = serie.dropna().round(2)
    return int(s.value_counts().iloc[0]) if len(s) else 0


def num(rotulo: str, ajuda: str | None = None):
    return st.column_config.NumberColumn(rotulo, format="localized", help=ajuda)


def barras(tabela: pd.DataFrame, rotulo: str, valor: str, titulo_valor: str, formato_hover: str) -> None:
    """Ranking em barras horizontais (maior em cima), uma cor só, valores no hover."""
    dados = tabela.copy()
    dados["_eixo"] = dados[rotulo].map(curto)
    dados = dados.iloc[::-1]  # plotly desenha de baixo para cima
    fig = px.bar(dados, x=valor, y="_eixo", orientation="h", custom_data=[rotulo])
    fig.update_traces(
        marker_color=AZUL, marker_line_width=0, marker_cornerradius=4,
        hovertemplate=f"<b>%{{customdata[0]}}</b><br>{titulo_valor}: %{{x:{formato_hover}}}<extra></extra>",
    )
    fig.update_layout(
        height=max(260, 26 * len(dados) + 60), margin=dict(l=0, r=10, t=10, b=0),
        xaxis_title=titulo_valor, yaxis_title=None, bargap=0.3, separators=",.",
        xaxis=dict(showgrid=True, gridcolor="rgba(137,135,129,0.25)"),
    )
    st.plotly_chart(fig, width="stretch")


COLUNAS_LICITACOES = ["publicacao", "numero_processo", "edital", "orgao", "abertura", "especificacao", "descricao",
                      "marca_bi", "quantidade", "valor_estimado", "valor_lance", "total_lance", "fornecedor", "status_item"]
CONFIG_LICITACOES = {
    "publicacao": st.column_config.TextColumn("Licitação (publicação)", help="Nº da publicação no Licita-Web"),
    "numero_processo": "Nº Processo", "edital": "Nº Edital", "orgao": "Órgão",
    "abertura": st.column_config.DatetimeColumn("Abertura", format="DD/MM/YYYY"),
    "especificacao": "Especificação", "descricao": "Descrição", "marca_bi": "Marca",
    "quantidade": num("Qtd"), "valor_estimado": num("Valor est. (R$)"), "valor_lance": num("Valor ganho (R$)"),
    "total_lance": num("Total ganho (R$)"), "fornecedor": "Quem ganhou", "status_item": "Status",
}


def tabela_licitacoes(dados: pd.DataFrame, chave: str) -> None:
    """Lista de ocorrências com o nº da licitação e todos os dados, mais recentes primeiro."""
    st.dataframe(dados.sort_values("abertura", ascending=False)[COLUNAS_LICITACOES], hide_index=True,
                 width="stretch", height=min(420, 38 + 35 * len(dados)), column_config=CONFIG_LICITACOES, key=chave)


def escolher(rotulo: str, valores, legenda, **kwargs):
    """Selectbox que mostra `legenda(valor)` e devolve o valor.

    As opções são os próprios textos exibidos. Sem `key`, quando a lista muda (outro nível ou
    filtro), a seleção volta para a primeira opção em vez de apontar para um item que sumiu.
    """
    mapa = {legenda(v): v for v in valores}
    texto = st.selectbox(rotulo, list(mapa), **kwargs)
    return None if texto is None else mapa[texto]


def linha_clicada(evento, tabela: pd.DataFrame):
    """Linha selecionada num st.dataframe com on_select (ou None)."""
    linhas = evento.selection.rows if evento is not None else []
    return tabela.iloc[linhas[0]] if linhas else None


# ---------------------------------------------------------------- página

st.set_page_config(page_title="BI Cotações SEFAZ-CE", page_icon="📊", layout="wide")
st.title("📊 Cotações finalizadas — SEFAZ-CE")

if not ARQUIVO.exists():
    st.error(f"Arquivo `{ARQUIVO.name}` não encontrado. Rode `python finalizadas.py` primeiro.")
    st.stop()

base = carregar(str(ARQUIVO), ARQUIVO.stat().st_mtime)
st.caption(f"Fonte: `{ARQUIVO.name}` · {inteiro(base['publicacao'].nunique())} cotações · "
           f"{inteiro(len(base))} itens · atualizado em {pd.Timestamp(ARQUIVO.stat().st_mtime, unit='s'):%d/%m/%Y %H:%M}")

# ---- filtros (valem para todas as abas, exceto 🔎 Licitação)
with st.sidebar:
    st.header("Filtros")
    dmin, dmax = base["abertura"].min().date(), base["abertura"].max().date()
    periodo = st.date_input("Abertura das propostas", (dmin, dmax), min_value=dmin, max_value=dmax, format="DD/MM/YYYY")
    busca = st.text_input("Buscar no item", placeholder="ex.: sabão, pó  ·  saco, 100 litros, preto",
                          help="Procura na descrição e na embalagem. Separe por vírgula para exigir vários termos.")
    f_produto = st.multiselect("Produto e tipo", sorted(base["produto_tipo"].unique()),
                               help="Ex.: SABAO (PO), SABAO (BARRA), VASSOURA (NYLON)")
    opcoes_espec = base[base["produto_tipo"].isin(f_produto)] if f_produto else base
    f_espec = st.multiselect("Especificação", sorted(opcoes_espec["especificacao"].unique()),
                             help="Escolha um produto acima para a lista ficar menor")
    f_marca = st.multiselect("Marca", sorted(base["marca_bi"].unique()))
    f_fornecedor = st.multiselect("Fornecedor", sorted(base["fornecedor"].unique()))
    f_orgao = st.multiselect("Órgão", sorted(base["orgao"].unique()))
    incluir_sem_vencedor = st.checkbox("Incluir itens sem vencedor (fracassados)", value=False)

df = base
if isinstance(periodo, tuple) and len(periodo) == 2:
    df = df[(df["abertura"].dt.date >= periodo[0]) & (df["abertura"].dt.date <= periodo[1])]
if busca:
    for termo in normalizar(busca).split(","):
        if termo.strip():
            df = df[df["busca"].str.contains(termo.strip(), regex=False)]
for coluna, escolhidos in [("produto_tipo", f_produto), ("especificacao", f_espec), ("marca_bi", f_marca),
                           ("fornecedor", f_fornecedor), ("orgao", f_orgao)]:
    if escolhidos:
        df = df[df[coluna].isin(escolhidos)]
if not incluir_sem_vencedor:
    df = df[df["vencido"]]

if df.empty:
    st.warning("Nenhum item com esses filtros.")
    st.stop()

# ---- indicadores
estimado, contratado = df["total_estimado"].sum(), df["total_lance"].sum()
k = st.columns(6)
k[0].metric("Cotações", inteiro(df["publicacao"].nunique()))
k[1].metric("Itens", inteiro(len(df)))
k[2].metric("Fornecedores", inteiro(df.loc[df["vencido"], "fornecedor"].nunique()))
k[3].metric("Valor estimado", brl(estimado))
k[4].metric("Valor contratado", brl(contratado))
k[5].metric("Economia", pct(1 - contratado / estimado) if estimado else "-",
            help="Quanto o valor contratado ficou abaixo do estimado: 1 − contratado ÷ estimado.")

abas = st.tabs(["🏆 Fornecedores", "📦 Itens", "🥇 Campeões", "🏷️ Marcas", "🔗 Relações",
                "📈 Preços", "🔎 Licitação", "📄 Dados"])
vencidos = df[df["vencido"]]

# ---------------------------------------------------------------- Fornecedores
with abas[0]:
    c1, c2 = st.columns([3, 1])
    metrica = c2.radio("Ranking por", ["Cotações ganhas", "Itens ganhos", "Valor ganho (R$)"], key="forn_metrica")
    top = c2.slider("Quantos mostrar", 5, 40, 15, key="forn_top")
    tabela = (vencidos.groupby("fornecedor")
              .agg(cotacoes=("publicacao", "nunique"), itens=("item", "size"), valor=("total_lance", "sum"),
                   desconto=("desconto", "mean"), marcas=("marca_bi", "nunique"),
                   marca_principal=("marca_bi", mais_frequente), produto_principal=("produto_tipo", mais_frequente),
                   orgaos=("orgao", "nunique"))
              .reset_index())
    coluna = {"Cotações ganhas": "cotacoes", "Itens ganhos": "itens", "Valor ganho (R$)": "valor"}[metrica]
    tabela = tabela.sort_values(coluna, ascending=False).reset_index(drop=True)
    with c1:
        barras(tabela.head(top), "fornecedor", coluna, metrica, ",.2f" if coluna == "valor" else ",.0f")
    st.caption("👆 Clique numa linha da tabela para ver as licitações que o fornecedor ganhou.")
    evento = st.dataframe(tabela, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
                          key="forn_tabela", column_config={
        "fornecedor": "Fornecedor", "cotacoes": "Cotações ganhas", "itens": "Itens ganhos",
        "valor": num("Valor ganho (R$)"),
        "desconto": st.column_config.NumberColumn("Desconto médio", format="percent",
                                                  help="Média de (1 − lance ÷ estimado) por item"),
        "marcas": "Nº de marcas", "marca_principal": "Marca mais usada",
        "produto_principal": "Produto mais vendido", "orgaos": "Nº de órgãos atendidos",
    })
    if (sel := linha_clicada(evento, tabela)) is not None:
        st.markdown(f"**Licitações ganhas por {sel['fornecedor']}**")
        tabela_licitacoes(vencidos[vencidos["fornecedor"] == sel["fornecedor"]], "forn_licitacoes")

# ---------------------------------------------------------------- Itens
with abas[1]:
    c1, c2 = st.columns([3, 1])
    nivel = c2.radio("Agrupar por", list(NIVEIS_ITEM), index=2, key="itens_nivel", help=AJUDA_NIVEIS)
    metrica = c2.radio("Ranking por", ["Vezes que saiu", "Quantidade total", "Valor contratado (R$)"], key="itens_metrica")
    top = c2.slider("Quantos mostrar", 5, 40, 15, key="itens_top")
    chave = NIVEIS_ITEM[nivel]
    tabela = (df.groupby(chave)
              .agg(vezes=("item", "size"), cotacoes=("publicacao", "nunique"), quantidade=("quantidade", "sum"),
                   valor=("total_lance", "sum"), preco_medio=("valor_lance", "mean"),
                   preco_min=("valor_lance", "min"), preco_max=("valor_lance", "max"),
                   marca_principal=("marca_bi", mais_frequente), fornecedor_principal=("fornecedor", mais_frequente))
              .reset_index())
    coluna = {"Vezes que saiu": "vezes", "Quantidade total": "quantidade", "Valor contratado (R$)": "valor"}[metrica]
    tabela = tabela.sort_values(coluna, ascending=False).reset_index(drop=True)
    with c1:
        barras(tabela.head(top), chave, coluna, metrica, ",.2f" if coluna == "valor" else ",.0f")
    if chave in ("produto", "produto_tipo"):
        st.caption("⚠️ Neste nível os preços misturam tamanhos e embalagens diferentes (ex.: pacote com 10 e com 100). "
                   "Para comparar preço, use **Especificação** ou as abas **Campeões** e **Preços**.")
    st.caption("👆 Clique numa linha da tabela para ver em quais licitações o item saiu.")
    evento = st.dataframe(tabela, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
                          key="itens_tabela", column_config={
        chave: nivel, "vezes": "Vezes que saiu", "cotacoes": "Em quantas cotações",
        "quantidade": num("Quantidade total"), "valor": num("Valor contratado (R$)"),
        "preco_medio": num("Preço unit. médio (R$)"), "preco_min": num("Preço unit. mín. (R$)"),
        "preco_max": num("Preço unit. máx. (R$)"),
        "marca_principal": "Marca mais frequente", "fornecedor_principal": "Fornecedor mais frequente",
    })
    if (sel := linha_clicada(evento, tabela)) is not None:
        st.markdown(f"**Licitações com {sel[chave]}**")
        tabela_licitacoes(df[df[chave] == sel[chave]], "itens_licitacoes")

# ---------------------------------------------------------------- Campeões
with abas[2]:
    st.markdown("Escolha um item e veja **qual marca mais ganhou**, **por qual preço ganhou com mais frequência** "
                "e **todas as vitórias** (licitação, órgão, quem ganhou, quantidade e valores).")
    c1, c2 = st.columns([1, 3])
    nivel_c = c1.radio("Escolher item por", ["Produto e tipo", "Especificação", "Produto"], key="camp_nivel",
                       help=AJUDA_NIVEIS)
    chave_c = NIVEIS_ITEM[nivel_c]
    contagem = vencidos[chave_c].value_counts()
    if contagem.empty:
        st.info("Nenhum item vencido com esses filtros.")
    else:
        with c2:
            item_c = escolher("Item para ver os campeões (digite para buscar · ordenado pelos que mais saíram)",
                              contagem.index,
                              lambda d: f"{curto(d, 110)}  ({contagem[d]}x)")
        dc = vencidos[vencidos[chave_c] == item_c]
        # Preço só é comparável dentro da mesma especificação (mesma embalagem): agrupa por marca + especificação
        campeoes = (dc.groupby(["marca_bi", "especificacao"])
                    .agg(vitorias=("item", "size"), cotacoes=("publicacao", "nunique"),
                         preco_frequente=("valor_lance", preco_mais_frequente),
                         vezes_preco=("valor_lance", vezes_no_preco_mais_frequente),
                         preco_medio=("valor_lance", "mean"), preco_min=("valor_lance", "min"),
                         preco_max=("valor_lance", "max"), quantidade=("quantidade", "sum"),
                         valor=("total_lance", "sum"), fornecedor_principal=("fornecedor", mais_frequente),
                         ultima=("abertura", "max"))
                    .reset_index().sort_values(["vitorias", "valor"], ascending=False).reset_index(drop=True))
        lider = campeoes.iloc[0]
        m = st.columns(4)
        m[0].metric("Marca que mais ganhou", lider["marca_bi"], help=lider["especificacao"])
        m[1].metric("Vitórias", inteiro(lider["vitorias"]), help=f"em {inteiro(lider['cotacoes'])} licitações")
        m[2].metric("Preço com que mais ganhou", brl(lider["preco_frequente"]),
                    help=f"{lider['vezes_preco']} vitórias exatamente nesse preço · "
                         f"média {brl(lider['preco_medio'])} · de {brl(lider['preco_min'])} a {brl(lider['preco_max'])}")
        m[3].metric("Quem mais ganhou com ela", curto(lider["fornecedor_principal"], 28), help=lider["fornecedor_principal"])
        st.caption(f"Campeã: **{lider['marca_bi']}** em **{lider['especificacao']}**")

        rotulos = campeoes.head(15).assign(rotulo=lambda t: t["marca_bi"] + " · " + t["especificacao"])
        barras(rotulos, "rotulo", "vitorias", "Vitórias", ",.0f")

        st.caption("👆 Clique numa linha para ver só as vitórias daquela marca. Sem seleção, a lista abaixo mostra todas.")
        evento = st.dataframe(campeoes, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
                              key="camp_tabela", column_config={
            "marca_bi": "Marca", "especificacao": "Especificação", "vitorias": "Vitórias",
            "cotacoes": "Licitações",
            "preco_frequente": num("Preço mais frequente (R$)", "Preço unitário com que mais vezes ganhou"),
            "vezes_preco": st.column_config.NumberColumn("Vezes nesse preço"),
            "preco_medio": num("Preço médio (R$)"), "preco_min": num("Mín. (R$)"), "preco_max": num("Máx. (R$)"),
            "quantidade": num("Qtd total"), "valor": num("Valor total (R$)"),
            "fornecedor_principal": "Quem mais ganhou",
            "ultima": st.column_config.DatetimeColumn("Última vitória", format="DD/MM/YYYY"),
        })
        sel = linha_clicada(evento, campeoes)
        if sel is not None:
            vitorias = dc[(dc["marca_bi"] == sel["marca_bi"]) & (dc["especificacao"] == sel["especificacao"])]
            titulo = f"{sel['marca_bi']} · {sel['especificacao']}"
        else:
            vitorias, titulo = dc, item_c
        e, d = st.columns([3, 1])
        with e:
            st.markdown(f"**Todas as vitórias — {titulo}** ({inteiro(len(vitorias))})")
            tabela_licitacoes(vitorias, "camp_licitacoes")
        with d:
            st.markdown("**Preços com que ganhou**")
            precos = (vitorias["valor_lance"].round(2).value_counts().rename_axis("preco").reset_index(name="vezes")
                      .sort_values(["vezes", "preco"], ascending=[False, True]))
            st.dataframe(precos, hide_index=True, width="stretch", height=min(420, 38 + 35 * len(precos)),
                         column_config={"preco": num("Preço (R$)"), "vezes": "Vezes"})

# ---------------------------------------------------------------- Marcas
with abas[3]:
    c1, c2 = st.columns([3, 1])
    metrica = c2.radio("Ranking por", ["Vezes que saiu", "Valor contratado (R$)"], key="marca_metrica")
    top = c2.slider("Quantos mostrar", 5, 40, 15, key="marca_top")
    com_marca = vencidos[vencidos["marca_bi"] != "(sem marca)"]
    ranking = (com_marca.groupby("marca_bi")
               .agg(vezes=("item", "size"), valor=("total_lance", "sum"), produtos=("produto_tipo", "nunique"),
                    fornecedores=("fornecedor", "nunique"))
               .reset_index())
    coluna = "vezes" if metrica == "Vezes que saiu" else "valor"
    ranking = ranking.sort_values(coluna, ascending=False)
    with c1:
        barras(ranking.head(top), "marca_bi", coluna, metrica, ",.2f" if coluna == "valor" else ",.0f")

    st.subheader("Detalhar uma marca")
    if ranking.empty:
        st.info("Nenhuma marca com esses filtros.")
    else:
        # sem key: se os filtros mudarem a lista de marcas, a seleção volta para a 1ª em vez de apontar para uma que sumiu
        marca = st.selectbox("Marca", ranking["marca_bi"], help="Lista ordenada pelo ranking acima")
        dm = com_marca[com_marca["marca_bi"] == marca]
        m = st.columns(4)
        m[0].metric("Vezes que saiu", inteiro(len(dm)))
        m[1].metric("Valor contratado", brl(dm["total_lance"].sum()))
        m[2].metric("Fornecedores que usaram", inteiro(dm["fornecedor"].nunique()))
        m[3].metric("Órgãos que compraram", inteiro(dm["orgao"].nunique()))
        e, d = st.columns(2)
        with e:
            st.markdown("**Em quais produtos aparece**")
            barras(dm["produto_tipo"].value_counts().head(10).rename_axis("produto_tipo").reset_index(name="vezes"),
                   "produto_tipo", "vezes", "Vezes", ",.0f")
        with d:
            st.markdown("**Quais fornecedores ofertaram**")
            barras(dm["fornecedor"].value_counts().head(10).rename_axis("fornecedor").reset_index(name="vezes"),
                   "fornecedor", "vezes", "Vezes", ",.0f")
        st.markdown("**Itens dessa marca e preços**")
        st.dataframe(
            dm.groupby("especificacao").agg(vezes=("item", "size"), preco_frequente=("valor_lance", preco_mais_frequente),
                                            preco_medio=("valor_lance", "mean"), preco_min=("valor_lance", "min"),
                                            preco_max=("valor_lance", "max"),
                                            fornecedores=("fornecedor", lambda s: ", ".join(s.value_counts().index[:3])))
              .reset_index().sort_values("vezes", ascending=False),
            hide_index=True, width="stretch", column_config={
                "especificacao": "Especificação", "vezes": "Vezes",
                "preco_frequente": num("Preço mais frequente (R$)"), "preco_medio": num("Preço médio (R$)"),
                "preco_min": num("Mín. (R$)"), "preco_max": num("Máx. (R$)"), "fornecedores": "Principais fornecedores",
            })
        with st.expander(f"Ver as {inteiro(len(dm))} licitações com a marca {marca}"):
            tabela_licitacoes(dm, "marca_licitacoes")

# ---------------------------------------------------------------- Relações
with abas[4]:
    st.markdown("Cruze dois campos para ver como eles se relacionam "
                "(ex.: **Fornecedor × Marca**, **Produto e tipo × Marca**, **Órgão × Fornecedor**).")
    c = st.columns(5)
    linhas_dim = c[0].selectbox("Linhas", list(DIMENSOES), index=0, key="rel_linhas")
    colunas_dim = c[1].selectbox("Colunas", list(DIMENSOES), index=1, key="rel_colunas")
    metrica = c[2].selectbox("Medida", ["Vezes que saiu", "Valor contratado (R$)", "Quantidade total"], key="rel_metrica")
    n_linhas = c[3].slider("Linhas (top)", 5, 30, 12, key="rel_nl")
    n_colunas = c[4].slider("Colunas (top)", 5, 30, 12, key="rel_nc")
    if linhas_dim == colunas_dim:
        st.info("Escolha dois campos diferentes.")
    else:
        li, co = DIMENSOES[linhas_dim], DIMENSOES[colunas_dim]
        medida = {"Vezes que saiu": ("item", "size"), "Valor contratado (R$)": ("total_lance", "sum"),
                  "Quantidade total": ("quantidade", "sum")}[metrica]
        top_li = vencidos.groupby(li)[medida[0]].agg(medida[1]).nlargest(n_linhas).index
        top_co = vencidos.groupby(co)[medida[0]].agg(medida[1]).nlargest(n_colunas).index
        sub = vencidos[vencidos[li].isin(top_li) & vencidos[co].isin(top_co)]
        pivo = (sub.pivot_table(index=li, columns=co, values=medida[0], aggfunc=medida[1], fill_value=0)
                .reindex(index=top_li, columns=top_co, fill_value=0))
        fig = px.imshow(pivo.values, x=[curto(str(v), 25) for v in pivo.columns], y=[curto(str(v), 40) for v in pivo.index],
                        color_continuous_scale=ESCALA_SEQUENCIAL, aspect="auto")
        formato = ",.2f" if medida[1] == "sum" else ",.0f"
        fig.update_traces(
            customdata=[[[str(r), str(col)] for col in pivo.columns] for r in pivo.index],
            hovertemplate=f"<b>%{{customdata[0]}}</b> × <b>%{{customdata[1]}}</b><br>{metrica}: %{{z:{formato}}}<extra></extra>",
            xgap=2, ygap=2,
        )
        fig.update_layout(height=max(320, 30 * len(pivo) + 140), margin=dict(l=0, r=0, t=10, b=0),
                          separators=",.", coloraxis_colorbar_title=None, xaxis_title=None, yaxis_title=None)
        fig.update_xaxes(side="top", tickangle=-35)
        st.plotly_chart(fig, width="stretch")
        st.caption(f"Mostrando os {len(top_li)} maiores em **{linhas_dim}** e os {len(top_co)} maiores em "
                   f"**{colunas_dim}** pela medida escolhida. Células vazias = nenhuma ocorrência.")
        with st.expander("Ver como tabela"):
            st.dataframe(pivo, width="stretch")

# ---------------------------------------------------------------- Preços
with abas[5]:
    c1, c2 = st.columns([1, 3])
    nivel_preco = c1.radio("Comparar por", ["Especificação", "Descrição completa"], key="preco_nivel",
                           help="Especificação junta descrições com mesmo produto, tipo, capacidade, cor e embalagem.")
    chave_preco = NIVEIS_ITEM[nivel_preco]
    contagem = vencidos[chave_preco].value_counts()
    if contagem.empty:
        st.info("Nenhum item vencido com esses filtros.")
    else:
        with c2:
            escolhido = escolher("Item para comparar preços (digite para buscar · ordenado pelos que mais saíram)",
                                 contagem.index,
                                 lambda d: f"{curto(d, 110)}  ({contagem[d]}x)")
        dp = vencidos[vencidos[chave_preco] == escolhido].copy()
        if chave_preco == "especificacao" and dp["descricao"].nunique() > 1:
            with st.expander(f"{dp['descricao'].nunique()} descrições do catálogo nesta especificação"):
                st.write(dp["descricao"].value_counts())
        else:
            st.caption(dp["descricao"].iloc[0])
        m = st.columns(4)
        m[0].metric("Vezes que saiu", inteiro(len(dp)))
        m[1].metric("Preço unit. médio", brl(dp["valor_lance"].mean()))
        m[2].metric("Mais barato", brl(dp["valor_lance"].min()))
        m[3].metric("Mais caro", brl(dp["valor_lance"].max()))

        top3 = dp["marca_bi"].value_counts().index[:3]
        dp["marca_grafico"] = dp["marca_bi"].where(dp["marca_bi"].isin(top3), "Outras")
        ordem = list(top3) + (["Outras"] if (dp["marca_grafico"] == "Outras").any() else [])
        cores = {m_: CATEGORICAS[i] for i, m_ in enumerate(top3)} | {"Outras": CINZA_OUTROS}
        fig = px.scatter(dp.sort_values("abertura"), x="abertura", y="valor_lance", color="marca_grafico",
                         category_orders={"marca_grafico": ordem}, color_discrete_map=cores,
                         custom_data=["marca_bi", "fornecedor", "orgao", "quantidade", "publicacao"])
        fig.update_traces(marker=dict(size=9, line=dict(width=1.5, color="white")),
                          hovertemplate="<b>%{customdata[0]}</b> · R$ %{y:,.2f}<br>Licitação %{customdata[4]} · "
                                        "%{x|%d/%m/%Y}<br>%{customdata[1]}<br>%{customdata[2]}<br>"
                                        "Qtd: %{customdata[3]:,.0f}<extra></extra>")
        fig.update_layout(height=420, margin=dict(l=0, r=0, t=10, b=0), separators=",.",
                          xaxis_title="Abertura das propostas", yaxis_title="Preço unitário do lance (R$)",
                          legend_title="Marca", yaxis=dict(rangemode="tozero", gridcolor="rgba(137,135,129,0.25)"))
        st.plotly_chart(fig, width="stretch")

        st.markdown("**Preço por marca** (qual marca sai mais barata para este item)")
        st.dataframe(
            dp.groupby("marca_bi").agg(vezes=("item", "size"), preco_frequente=("valor_lance", preco_mais_frequente),
                                       preco_medio=("valor_lance", "mean"), preco_min=("valor_lance", "min"),
                                       preco_max=("valor_lance", "max"),
                                       fornecedores=("fornecedor", lambda s: ", ".join(s.value_counts().index[:3])))
              .reset_index().sort_values("preco_medio"),
            hide_index=True, width="stretch", column_config={
                "marca_bi": "Marca", "vezes": "Vezes", "preco_frequente": num("Preço mais frequente (R$)"),
                "preco_medio": num("Preço médio (R$)"), "preco_min": num("Mín. (R$)"), "preco_max": num("Máx. (R$)"),
                "fornecedores": "Fornecedores",
            })
        with st.expander(f"Ver as {inteiro(len(dp))} licitações deste item"):
            tabela_licitacoes(dp, "preco_licitacoes")

# ---------------------------------------------------------------- Licitação
with abas[6]:
    st.markdown("Veja todos os dados de uma licitação. Digite o nº da publicação, do processo ou o nome do órgão. "
                "*(Esta aba ignora os filtros da barra lateral.)*")
    cabecalhos = (base.drop_duplicates("publicacao").sort_values("abertura", ascending=False)
                  .set_index("publicacao"))
    rotulo = (cabecalhos.index + " · proc. " + cabecalhos["numero_processo"] + " · " + cabecalhos["orgao"]
              + " · " + cabecalhos["abertura"].dt.strftime("%d/%m/%Y"))
    pub = escolher("Licitação", cabecalhos.index, lambda p: rotulo[p], key="lic_pub", index=None,
                   placeholder="ex.: 2026/33907, 22001153486202658 ou o nome da escola")
    if pub is None:
        st.info("Escolha uma licitação acima.")
    else:
        dl = base[base["publicacao"] == pub]
        cab = dl.iloc[0]
        st.subheader(f"Licitação {pub} — {cab['orgao']}")
        m = st.columns(4)
        m[0].metric("Nº Processo", cab["numero_processo"])
        m[1].metric("Nº Edital", cab["edital"] or "-")
        m[2].metric("Início do acolhimento", f"{cab['acolhimento']:%d/%m/%Y %H:%M}" if pd.notna(cab["acolhimento"]) else "-")
        m[3].metric("Abertura das propostas", f"{cab['abertura']:%d/%m/%Y %H:%M}" if pd.notna(cab["abertura"]) else "-")
        est, con = dl["total_estimado"].sum(), dl["total_lance"].sum()
        m = st.columns(4)
        m[0].metric("Itens", inteiro(len(dl)))
        m[1].metric("Total estimado", brl(est))
        m[2].metric("Total contratado", brl(con))
        m[3].metric("Economia", pct(1 - con / est) if est else "-")
        vencedores = dl.loc[dl["vencido"], "fornecedor"].unique()
        st.markdown("**Vencedor(es):** " + (", ".join(vencedores) if len(vencedores) else "nenhum"))
        st.dataframe(dl[["grupo", "item", "descricao", "unidade_fornecimento", "quantidade", "valor_estimado",
                         "total_estimado", "valor_lance", "total_lance", "marca", "fornecedor", "status_item"]],
                     hide_index=True, width="stretch", column_config={
            "grupo": "Grupo", "item": "Item", "descricao": "Descrição", "unidade_fornecimento": "Unidade",
            "quantidade": num("Qtd"), "valor_estimado": num("Valor est. (R$)"), "total_estimado": num("Total est. (R$)"),
            "valor_lance": num("Valor lance (R$)"), "total_lance": num("Total lance (R$)"), "marca": "Marca",
            "fornecedor": "Quem ganhou", "status_item": "Status",
        })

# ---------------------------------------------------------------- Dados
with abas[7]:
    colunas = ["publicacao", "numero_processo", "edital", "orgao", "abertura", "produto_tipo", "especificacao", "descricao",
               "unidade_fornecimento", "quantidade", "valor_estimado", "valor_lance", "total_estimado", "total_lance",
               "desconto", "marca_bi", "fornecedor", "status_item"]
    st.dataframe(df[colunas], hide_index=True, width="stretch", height=520, column_config={
        **CONFIG_LICITACOES,
        "produto_tipo": "Produto e tipo", "unidade_fornecimento": "Unidade",
        "abertura": st.column_config.DatetimeColumn("Abertura", format="DD/MM/YYYY HH:mm"),
        "valor_lance": num("Valor lance (R$)"), "total_estimado": num("Total est. (R$)"),
        "total_lance": num("Total lance (R$)"), "fornecedor": "Fornecedor",
        "desconto": st.column_config.NumberColumn("Desconto", format="percent"),
    })
    st.download_button("Baixar estes dados (CSV)", df[colunas].to_csv(sep=";", decimal=",", index=False).encode("utf-8-sig"),
                       file_name="bi_filtrado.csv", mime="text/csv")
