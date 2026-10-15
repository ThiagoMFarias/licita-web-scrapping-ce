"""Coleta o resultado (vencedor, lances e itens) das cotações finalizadas.

Para cada publicação da lista, o script faz o mesmo que o usuário no site:
clica na linha, clica em "Visualizar", e na página da cotação clica em
"(expandir)" em cada grupo para ver os itens.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from scraper import FILTROS, FORM, URL, LicitaWeb, SiteIndisponivel, normalizar, parametros_ajax, verificar_disponivel

SITE = "https://s2gpr.sefaz.ce.gov.br"


class SemConexao(RuntimeError):
    """A rede (internet, VPN ou DNS) ficou fora por vários minutos seguidos."""


def numero(texto: str) -> float | None:
    """'1.234,5600' -> 1234.56"""
    texto = texto.strip()
    if not texto:
        return None
    try:
        return float(texto.replace(".", "").replace(",", "."))
    except ValueError:
        return None


def decodificar(resp) -> str:
    """O site mistura codificações: algumas páginas dizem ISO-8859-1 e mandam UTF-8, outras não."""
    try:
        texto = resp.content.decode("utf-8")
    except UnicodeDecodeError:
        texto = resp.content.decode("cp1252", errors="replace")
    verificar_disponivel(texto)
    return texto


def texto_celula(td) -> str:
    return td.get_text(" ", strip=True) if td else ""


def colunas(tabela) -> list[str]:
    """Nomes das colunas do cabeçalho próprio da tabela (ignora tabelas aninhadas)."""
    thead = tabela.find("thead", recursive=False)
    if thead is None:
        return []
    linha = thead.find("tr")
    return [normalizar(th.get_text(" ", strip=True)) for th in linha.find_all("th", recursive=False)]


def linhas(tabela):
    tbody = tabela.find("tbody", recursive=False)
    return tbody.find_all("tr", class_="rich-table-row", recursive=False) if tbody else []


def campo_visual(soup: BeautifulSoup, rotulo: str) -> str:
    """Lê os campos 'Rótulo: valor' da página da cotação."""
    alvo = normalizar(rotulo)
    for div in soup.select("div.prop > div.name"):
        if normalizar(div.get_text()).rstrip(":") == alvo:
            return texto_celula(div.find_next_sibling("div", class_="value"))
    return ""


def sem_vencedor(texto: str) -> str:
    return re.sub(r"^\s*Vencedor:\s*", "", texto)


class Detalhe:
    """Página Licitacao.seam de uma cotação."""

    def __init__(self, site: LicitaWeb, html: str):
        self.site = site
        self.soup = BeautifulSoup(html, "html.parser")
        form = self.soup.find("form", id=FORM)
        self.action = SITE + form["action"]
        self.campos = {}
        for el in form.find_all(["input", "textarea"]):
            nome, tipo = el.get("name"), (el.get("type") or "").lower()
            if nome and tipo not in ("button", "submit", "image", "radio", "checkbox"):
                self.campos[nome] = el.get("value", "")

    def ajax(self, params: dict[str, str]) -> BeautifulSoup:
        resp = self.site.http.post(self.action, data={"AJAXREQUEST": "_viewRoot", **self.campos, **params}, timeout=60)
        resp.raise_for_status()
        time.sleep(self.site.pausa)
        return BeautifulSoup(resp.text, "html.parser")

    def tabela_grupos(self, soup: BeautifulSoup):
        return soup.find("table", id=f"{FORM}:grupoItensCoEPDataTable")

    def extrair(self) -> dict:
        cab = {
            "numero_processo": campo_visual(self.soup, "Nº Processo"),
            "numero_edital": campo_visual(self.soup, "Nº do Edital"),
            "inicio_acolhimento": campo_visual(self.soup, "Acolhimento"),
            "abertura_propostas": campo_visual(self.soup, "Abertura"),
            "status": campo_visual(self.soup, "Status da Cotação"),
            "objeto": campo_visual(self.soup, "Objeto da Contratação"),
        }
        grupos = []
        tabela = self.tabela_grupos(self.soup)
        if tabela is not None:
            indices = sorted({int(m.group(1)) for td in tabela.find_all("td", id=True)
                              if (m := re.match(rf"{FORM}:grupoItensCoEPDataTable:(\d+):[^:]+$", td["id"]))})
            for i in indices:
                grupos.append(self.extrair_grupo(i))
        # Cotações julgadas por item não têm grupos: os itens ficam numa tabela própria.
        soltos = self.soup.find("table", id=f"{FORM}:itemCoEPDataTable")
        if soltos is not None:
            grupos.append(self.extrair_itens_sem_grupo(soltos))
        if not any(g["itens"] for g in grupos):
            raise RuntimeError("nenhum item encontrado na página da cotação")
        cab["total_estimado"] = round(sum(g["total_estimado"] or 0 for g in grupos), 4)
        cab["total_lance"] = round(sum(g["total_lance"] or 0 for g in grupos), 4)
        cab["grupos"] = grupos
        return cab

    def extrair_itens_sem_grupo(self, tabela) -> dict:
        """Itens de cotação julgada por item (cada item tem seu próprio vencedor).

        A página da cotação não mostra a marca desses itens; ela vem de
        'Visualizar Propostas' na página pública de resultado.
        """
        itens = [self.extrair_item(colunas(tabela), tr) for tr in linhas(tabela)]
        propostas = self.propostas_vencedoras()
        for item in itens:
            # Itens fracassados/desertos não têm vencedor nem marca: ficam com marca vazia.
            info = propostas.get(item["item"], {})
            item["marca"] = info.get("marca", "")
            item["status_item"] = item["status_item"] or info.get("status", "")
        vencedores = list(dict.fromkeys(i["fornecedor"] for i in itens if i["fornecedor"]))
        return {
            "grupo": "",
            "identificador": "itens sem grupo (julgamento por item)",
            "fornecedor_vencedor": " / ".join(vencedores),
            "total_estimado": round(sum(i["total_estimado"] or 0 for i in itens), 4),
            "total_lance": round(sum(i["total_lance"] or 0 for i in itens), 4),
            "itens": itens,
        }

    def propostas_vencedoras(self) -> dict[str, dict]:
        """{nº do item: {'status', 'marca'}}, via página pública de resultado.

        O status de todos os itens vem da própria tabela. Para os itens que têm
        vencedor: abre a página, seleciona o item e clica em 'Visualizar Propostas'
        para ler a marca.
        """
        m = re.search(r"VisualizarResultadoCoep\.seam\?coepId=(\d+)", str(self.soup))
        if not m:
            raise RuntimeError("link 'Visualizar Propostas' não encontrado")
        url = f"{SITE}/cotacao-web/paginas/proposta/VisualizarResultadoCoep.seam?coepId={m.group(1)}"
        form_id = "itensDesagrupadosForm"
        resultado, i = {}, 0
        while True:
            soup = BeautifulSoup(self.site._get(url), "html.parser")
            form = soup.find("form", id=form_id)
            selecionar = form.find(id=f"{form_id}:itemCoEPDataTable:{i}:panelDeCheckSingleItem") if form else None
            if selecionar is None:
                return resultado
            i += 1
            linha = selecionar.find_parent("tr")
            cols = [normalizar(th.get_text(" ", strip=True)) for th in form.select(f"table[id='{form_id}:itemCoEPDataTable'] > thead th")]
            tds = linha.find_all("td", recursive=False)
            celula = lambda nome: texto_celula(tds[cols.index(nome)]) if nome in cols else ""
            numero_item = celula("ITEM").replace(" ", "")
            resultado[numero_item] = {"status": celula("STATUS"), "marca": ""}
            if not sem_vencedor(celula("FORNECEDOR")):
                continue  # fracassado/deserto: não há proposta vencedora para abrir

            acao = SITE + form["action"]
            campos = {form_id: form_id, "javax.faces.ViewState": form.find("input", attrs={"name": "javax.faces.ViewState"})["value"]}
            post = lambda params: BeautifulSoup(decodificar(self.site.http.post(
                acao, data={"AJAXREQUEST": "_viewRoot", **campos, **params}, timeout=60)), "html.parser")
            resp = post(parametros_ajax(selecionar["onclick"]))
            botao = next((b for b in resp.find_all("input", attrs={"type": "button"})
                          if b.get("value") == "Visualizar Propostas" and b.get("onclick", "").startswith("A4J")), None)
            if botao is None:
                raise RuntimeError(f"'Visualizar Propostas' não habilitou para o item {numero_item}")
            resp = post(parametros_ajax(botao["onclick"]))
            time.sleep(self.site.pausa)

            tabela = resp.find("table", id="controlesGerais:melhorPropostaTable")
            if tabela is None or not linhas(tabela):
                raise RuntimeError(f"proposta vencedora do item {numero_item} não encontrada")
            cols_p = colunas(tabela)
            tds_p = linhas(tabela)[0].find_all("td", recursive=False)
            valor = lambda nome: texto_celula(tds_p[cols_p.index(nome)]) if nome in cols_p else ""
            resultado[numero_item]["marca"] = valor("MARCA").split("Inf. compl.:")[0].strip()

    @staticmethod
    def linha_grupo(tabela, indice: int):
        """Linha do grupo pelo índice no ID das células (as linhas de itens expandidos ficam no meio)."""
        prefixo = f"{FORM}:grupoItensCoEPDataTable:{indice}:"
        for tr in linhas(tabela):
            td = tr.find("td", recursive=False)
            if td is not None and td.get("id", "").startswith(prefixo):
                return tr
        raise RuntimeError(f"Grupo {indice} não encontrado.")

    def extrair_grupo(self, indice: int) -> dict:
        tr = self.linha_grupo(self.tabela_grupos(self.soup), indice)
        link = tr.find("a", string=re.compile("expandir"))
        soup = self.ajax(parametros_ajax(link["onclick"])) if link else self.soup
        tabela = self.tabela_grupos(soup)
        tr = self.linha_grupo(tabela, indice)

        cols = colunas(tabela)
        tds = tr.find_all("td", recursive=False)
        get = lambda nome: texto_celula(tds[cols.index(nome)]) if nome in cols and cols.index(nome) < len(tds) else ""
        grupo = {
            "grupo": re.sub(r"\((expandir|retrair)\)", "", get("GRUPO")).strip(),
            "identificador": get("IDENTIFICADOR"),
            "fornecedor_vencedor": sem_vencedor(get("FORNECEDOR")),
            "total_estimado": numero(get("TOTAL ESTIMADO")),
            "total_lance": numero(get("TOTAL LANCE")),
            "itens": [],
        }

        itens = soup.find("table", id=f"{FORM}:grupoItensCoEPDataTable:{indice}:itensGrupoListAction")
        if itens is not None:
            grupo["itens"] = [self.extrair_item(colunas(itens), linha) for linha in linhas(itens)]
        return grupo

    @staticmethod
    def extrair_item(cols: list[str], tr) -> dict:
        tds = tr.find_all("td", recursive=False)
        celula = lambda nome: tds[cols.index(nome)] if nome in cols and cols.index(nome) < len(tds) else None

        desc_td = celula("DESCRICAO DO ITEM")
        descricao, unidade = "", ""
        if desc_td is not None:
            dica = desc_td.select_one("span.rich-tool-tip span[id$='content']")  # texto completo
            descricao = texto_celula(dica) if dica else texto_celula(desc_td)
            m = re.search(r"Und\. Forn\.:\s*(.*?)\s*(Servi[çc]o:|Item c/|Cod\. Cat\.|Obs:|$)", texto_celula(desc_td))
            unidade = m.group(1) if m else ""

        return {
            "item": texto_celula(celula("ITEM")).replace(" ", ""),
            "descricao": descricao,
            "unidade_fornecimento": unidade,
            "fornecedor": sem_vencedor(texto_celula(celula("FORNECEDOR"))),
            "quantidade": numero(texto_celula(celula("QTDE."))),
            "valor_estimado": numero(texto_celula(celula("VALOR UNIT. ESTIMADO"))),
            "total_estimado": numero(texto_celula(celula("VALOR TOTAL ESTIMADO"))),
            "valor_lance": numero(texto_celula(celula("MELHOR LANCE"))),
            "total_lance": numero(texto_celula(celula("TOTAL MELHOR LANCE"))),
            "marca": texto_celula(celula("MARCA")),
            "status_item": texto_celula(celula("STATUS")),
        }


class ColetorFinalizadas(LicitaWeb):
    """Abre cada cotação a partir da lista, como no navegador.

    O servidor guarda a lista e a cotação aberta numa mesma "conversação" (Seam):
    depois de abrir uma cotação, o 'Visualizar' passa a reabrir sempre ela.
    Por isso cada cotação é aberta numa conversação nova (filtros reaplicados).
    """

    def __init__(self, filtros: dict[str, str], pausa: float = 0.5):
        super().__init__(pausa=pausa)
        self.filtros = filtros

    def _post_lista(self, params: dict[str, str]):
        r = self.http.post(URL, data={"AJAXREQUEST": "_viewRoot", **self.campos, **params}, timeout=60)
        r.raise_for_status()
        decodificar(r)  # levanta SiteIndisponivel se o site saiu do ar
        time.sleep(self.pausa)
        return r

    def _get(self, url: str) -> str:
        pagina = self.http.get(url, timeout=60)
        pagina.raise_for_status()
        time.sleep(self.pausa)
        return decodificar(pagina)

    def _pesquisar(self) -> BeautifulSoup:
        """Nova conversação: abre a página, aplica filtros e pesquisa (fica na página 1)."""
        self.abrir()
        self.aplicar_filtros(self.filtros)
        botao = self.soup.find("input", id=f"{FORM}:pesquisar")
        return self.ajax(parametros_ajax(botao["onclick"]))

    def _ir_para(self, pagina) -> BeautifulSoup:
        """Paginação da lista: 'next' ou o número da página (pula direto, sem passar pelas anteriores)."""
        return self.ajax({
            "ajaxSingle": f"{FORM}:datascrollerSuperior",
            f"{FORM}:datascrollerSuperior": str(pagina),
        })

    def paginas(self):
        """Pesquisa e percorre todas as páginas da lista."""
        resp = self._pesquisar()
        n = 1
        yield n, resp
        while self.tem_proxima(resp):
            n += 1
            resp = self._ir_para("next")
            yield n, resp

    def listar(self, limite: int | None = None, pular: set[str] = frozenset()) -> list[tuple[int, dict]]:
        """(página, registro) das cotações da busca, sem repetições e sem as de `pular`."""
        alvos, vistos, puladas = [], set(), 0
        for n, resp in self.paginas():
            for reg in self.extrair_linhas(resp):
                if reg["id_interno"] in vistos:
                    continue
                vistos.add(reg["id_interno"])
                if reg["Publicação Sequencial"] in pular:
                    puladas += 1
                else:
                    alvos.append((n, reg))
            print(f"  página {n}: {len(alvos)} a coletar, {puladas} já coletadas", file=sys.stderr)
            if limite and len(alvos) >= limite:
                return alvos[:limite]
        return alvos

    def abrir_cotacao(self, pagina: int, id_interno: str) -> Detalhe:
        """Vai até a página da cotação, clica na linha e depois em 'Visualizar'."""
        resp = self._pesquisar()
        if pagina > 1:
            resp = self._ir_para(pagina)
        tr = next((tr for tr in self._trs(resp) if f"pagedDataTable:{id_interno}:" in tr.get("onclick", "")), None)
        if tr is None:
            raise RuntimeError(f"cotação não encontrada na página {pagina} (a lista mudou?)")

        r = self._post_lista(parametros_ajax(tr["onclick"]))
        destino = r.headers.get("Location")
        if not destino:
            botao = BeautifulSoup(r.text, "html.parser").find("input", id=f"{FORM}:visualizarSuperior")
            if botao is None or not botao.get("onclick", "").startswith("A4J"):
                raise RuntimeError("Botão 'Visualizar' não ficou habilitado após selecionar a linha.")
            destino = self._post_lista(parametros_ajax(botao["onclick"])).headers.get("Location")
        if not destino:
            raise RuntimeError("O site não redirecionou para a página da cotação.")
        return Detalhe(self, self._get(SITE + destino))

    def abrir_com_rede_instavel(self, pagina: int, reg: dict, tentativas: int = 5, espera: int = 60) -> dict:
        """Abre e extrai a cotação; se a conexão cair, espera e tenta de novo a mesma cotação."""
        for tentativa in range(1, tentativas + 1):
            try:
                dados = self.abrir_cotacao(pagina, reg["id_interno"]).extrair()
                if dados["numero_processo"] != reg["Nº Processo"]:
                    raise RuntimeError(f"abriu o processo {dados['numero_processo']}, esperado {reg['Nº Processo']}")
                return dados
            except (requests.ConnectionError, requests.Timeout) as erro:
                if tentativa == tentativas:
                    raise SemConexao(f"Sem conexão com o site há ~{tentativas * espera // 60} min ({type(erro).__name__}).") from erro
                print(f"      sem conexão; nova tentativa em {espera}s ({tentativa}/{tentativas - 1})...", file=sys.stderr)
                time.sleep(espera)

    def coletar(self, limite: int | None = None, pular: set[str] = frozenset(), ao_coletar=None) -> tuple[list[dict], int]:
        """Coleta as cotações; `ao_coletar(cotacao)` é chamado logo após cada sucesso.

        Retorna (cotações coletadas, quantidade de erros). Ctrl+C interrompe e
        devolve o que já foi coletado.
        """
        alvos = self.listar(limite, pular)
        print(f"{len(alvos)} cotações para coletar.", file=sys.stderr)
        resultado, erros = [], 0
        try:
            for i, (pagina, reg) in enumerate(alvos, 1):
                pub = reg["Publicação Sequencial"]
                entrega = reg.get("Nº Edital - Contratante - Entrega", "").strip(" -")
                print(f"  [{i}/{len(alvos)}] {pub} - {entrega}", file=sys.stderr)
                try:
                    dados = self.abrir_com_rede_instavel(pagina, reg)
                except SemConexao as erro:  # rede fora por vários minutos: para em vez de falhar todas
                    erros += len(alvos) - i + 1
                    print(f"\n{erro}\nColeta interrompida; rode de novo quando a conexão voltar.", file=sys.stderr)
                    break
                except SiteIndisponivel as erro:  # não adianta continuar: todas as próximas falhariam
                    erros += len(alvos) - i + 1
                    print(f"\n{erro}\nColeta interrompida; rode de novo quando o site voltar.", file=sys.stderr)
                    break
                except Exception as erro:  # segue para as próximas se uma falhar
                    erros += 1
                    print(f"      ERRO: {erro}", file=sys.stderr)
                    continue
                cotacao = {"publicacao": pub, "contratante": entrega, "id_interno": reg["id_interno"], **dados}
                resultado.append(cotacao)
                if ao_coletar:
                    ao_coletar(cotacao)
        except KeyboardInterrupt:
            print("\nInterrompido pelo usuário; salvando o que já foi coletado.", file=sys.stderr)
            erros += 1  # trata como incompleto: mantém o arquivo parcial
        return resultado, erros

    @staticmethod
    def _trs(soup: BeautifulSoup):
        tabela = soup.find("table", id=f"{FORM}:pagedDataTable")
        return tabela.select("tbody tr.rich-table-row") if tabela else []


COLUNAS_CSV = [
    "publicacao", "numero_processo", "contratante", "inicio_acolhimento", "abertura_propostas",
    "total_estimado_licitacao", "grupo", "fornecedor_vencedor", "total_lance_vencedor",
    "item", "descricao", "unidade_fornecimento", "quantidade", "valor_estimado", "total_estimado",
    "valor_lance", "total_lance", "marca", "fornecedor_item", "status_item",
]


def para_linhas(cotacoes: list[dict]) -> list[dict]:
    """Uma linha por item, repetindo os dados da cotação e do grupo."""
    saida = []
    for c in cotacoes:
        base = {
            "publicacao": c["publicacao"],
            "numero_processo": c["numero_processo"],
            "contratante": c["contratante"],
            "inicio_acolhimento": c["inicio_acolhimento"],
            "abertura_propostas": c["abertura_propostas"],
            "total_estimado_licitacao": c["total_estimado"],
        }
        for g in c["grupos"]:
            grupo = {
                "grupo": g["grupo"],
                "fornecedor_vencedor": g["fornecedor_vencedor"],
                "total_lance_vencedor": g["total_lance"],
            }
            for it in g["itens"] or [{}]:
                item = {k: it.get(k) for k in COLUNAS_CSV if k in it}
                item["fornecedor_item"] = it.get("fornecedor")
                saida.append({**base, **grupo, **item})
    return saida


def formatar_br(valor) -> str:
    if isinstance(valor, float):
        return f"{valor:.4f}".replace(".", ",")
    return "" if valor is None else str(valor)


def ler_parcial(caminho: Path) -> list[dict]:
    """Cotações já coletadas numa execução anterior que não terminou."""
    if not caminho.exists():
        return []
    cotacoes = []
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        try:
            cotacoes.append(json.loads(linha))
        except json.JSONDecodeError:
            pass  # última linha pode ter ficado pela metade se o programa caiu durante a escrita
    return cotacoes


def carregar_anteriores(base: Path, parcial: Path) -> list[dict]:
    """Junta o resultado final anterior (.json) e o progresso parcial, sem repetir.

    Cotações sem nenhum item (coletadas por versões antigas do script) são
    descartadas para serem baixadas de novo.
    """
    final = base.with_suffix(".json")
    todas = json.loads(final.read_text(encoding="utf-8")) if final.exists() else []
    todas += ler_parcial(parcial)
    por_publicacao = {c["publicacao"]: c for c in todas}
    validas = {p: c for p, c in por_publicacao.items() if any(g["itens"] for g in c["grupos"])}
    if len(validas) < len(por_publicacao):
        print(f"{len(por_publicacao) - len(validas)} cotação(ões) sem itens serão coletadas de novo.", file=sys.stderr)
    return list(validas.values())


def salvar_final(cotacoes: list[dict], base: Path) -> None:
    base.parent.mkdir(parents=True, exist_ok=True)
    base.with_suffix(".json").write_text(json.dumps(cotacoes, ensure_ascii=False, indent=2), encoding="utf-8")
    with base.with_suffix(".csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLUNAS_CSV, delimiter=";")
        w.writeheader()
        for linha in para_linhas(cotacoes):
            w.writerow({k: formatar_br(v) for k, v in linha.items()})


def main() -> None:
    ap = argparse.ArgumentParser(description="Coleta vencedores e itens das cotações finalizadas.")
    ap.add_argument("-o", "--saida", default="finalizadas", help="nome base dos arquivos (gera .csv e .json)")
    ap.add_argument("-n", "--limite", type=int, help="máximo de cotações novas a coletar (útil para testes)")
    ap.add_argument("--recomecar", action="store_true", help="ignora o arquivo parcial e coleta tudo de novo")
    args = ap.parse_args()

    base = Path(args.saida)
    parcial = base.with_name(base.name + ".parcial.jsonl")
    if args.recomecar:
        parcial.unlink(missing_ok=True)
        anteriores = []
    else:
        anteriores = carregar_anteriores(base, parcial)
    if anteriores:
        print(f"Retomando: {len(anteriores)} cotação(ões) já coletadas.", file=sys.stderr)

    parcial.parent.mkdir(parents=True, exist_ok=True)
    with parcial.open("a", encoding="utf-8") as arq:
        def gravar(cotacao: dict) -> None:
            arq.write(json.dumps(cotacao, ensure_ascii=False) + "\n")
            arq.flush()  # grava em disco na hora, para não perder nada se cair

        site = ColetorFinalizadas({**FILTROS, "status": "Finalizada"})
        print("Listando cotações finalizadas...", file=sys.stderr)
        try:
            novas, erros = site.coletar(args.limite, {c["publicacao"] for c in anteriores}, gravar)
        except KeyboardInterrupt:  # Ctrl+C ainda durante a listagem
            novas, erros = [], 1
        except SiteIndisponivel as erro:  # fora do horário já na listagem
            print(f"\n{erro}", file=sys.stderr)
            novas, erros = [], 1
        except (requests.ConnectionError, requests.Timeout) as erro:  # rede fora já na listagem
            print(f"\nSem conexão com o site ({type(erro).__name__}). Verifique a internet/VPN e rode de novo.", file=sys.stderr)
            novas, erros = [], 1

    cotacoes = anteriores + novas
    salvar_final(cotacoes, base)
    n_itens = sum(len(g["itens"]) for c in cotacoes for g in c["grupos"])
    print(f"\n{len(cotacoes)} cotação(ões) ({len(novas)} novas), {n_itens} itens. "
          f"Salvo em {base.with_suffix('.csv')} e .json", file=sys.stderr)

    if erros:
        print(f"{erros} falha(s). Rode de novo para tentar só o que faltou "
              f"(o progresso está em {parcial}).", file=sys.stderr)
    elif args.limite:
        print(f"Coleta limitada com -n; rode de novo para continuar (progresso em {parcial}).", file=sys.stderr)
    else:
        parcial.unlink(missing_ok=True)  # coleta completa: o temporário não é mais necessário


if __name__ == "__main__":
    main()
