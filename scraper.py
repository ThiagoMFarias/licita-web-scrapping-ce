"""Web scraping das publicações do Licita-Web (SEFAZ-CE).

A página é JSF/RichFaces: os selects são encadeados via AJAX (Tipo depende de
Natureza, Forma depende de Sistemática, Sistemática depende de Regime), então
o script reproduz a mesma sequência de requisições que o navegador faz.

Campos e opções são localizados pelo texto visível (rótulos), e não por IDs
"j_idNNN", que são gerados automaticamente pelo JSF e podem mudar.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import unicodedata
from pathlib import Path

import requests
import truststore
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

# Usa os certificados do sistema operacional (necessário atrás de proxies corporativos).
truststore.inject_into_ssl()

URL = "https://s2gpr.sefaz.ce.gov.br/licita-web/paginas/licita/PublicacaoList.seam"
FORM = "formularioDeCrud"
NO_SELECTION = "org.jboss.seam.ui.NoSelectionConverter.noSelectionValue"

# Filtros da busca (texto exatamente como aparece no site; acentos/maiúsculas são ignorados).
FILTROS = {
    "natureza": "MATERIAL DE CONSUMO",
    "tipo": "MATERIAL DE LIMPEZA E PRODUÇÃO DE HIGIENIZAÇÃO",
    "regime": "Lei 14.133/21",
    "sistematica": "DISPENSA",
    "forma": "COTAÇÃO ELETRÔNICA",
    "status": "Publicada",
    "microrregiao": "Fortaleza",
}

# Sufixos dos IDs dos campos (estes são nomeados pelo desenvolvedor e estáveis).
SELECT_IDS = {
    "natureza": "naturezaAquisicaoDecoration:naturezaAquisicao",
    "tipo": "tipoAquisicaoDecoration:tipoAquisicao",
    "sistematica": "sistematicaAquisicaoDecoration:sistAquisicao",
    "forma": "formaAquisicaoDecoration:formaAquisicao",
    "status": "statusDecoration:status",
    "microrregiao": "microRegiaoDecoration:microRegiao",
}


class SiteIndisponivel(RuntimeError):
    """O Licita-Web tem horário de funcionamento; fora dele responde 'Serviço indisponível'."""


def verificar_disponivel(html: str) -> None:
    if len(html) < 2000 and "indispon" in normalizar(BeautifulSoup(html, "html.parser").get_text()):
        raise SiteIndisponivel("O site respondeu 'Serviço indisponível neste horário'. Tente mais tarde.")


def normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return " ".join(texto.upper().split())


def parametros_ajax(onchange: str) -> dict[str, str]:
    """Extrai o dict 'parameters' de um A4J.AJAX.Submit(...) do atributo onchange/onclick."""
    m = re.search(r"'parameters':\{([^}]*)\}", onchange)
    if not m:
        raise RuntimeError(f"Não encontrei parâmetros AJAX em: {onchange[:120]}")
    return dict(re.findall(r"'([^']+)':'([^']+)'", m.group(1)))


class LicitaWeb:
    def __init__(self, pausa: float = 0.5):
        self.http = requests.Session()
        # Quedas rápidas de rede/DNS: repete a requisição algumas vezes antes de desistir
        # (espera 0, 6, 12, 24 s). Só repete falhas de conexão, nunca respostas do site.
        repetir = Retry(total=4, connect=4, read=0, status=0, backoff_factor=3, allowed_methods=None)
        self.http.mount("https://", HTTPAdapter(max_retries=repetir))
        self.http.headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
        )
        self.pausa = pausa
        self.campos: dict[str, str] = {}
        self.soup: BeautifulSoup | None = None

    # ---------- infraestrutura ----------

    def abrir(self) -> None:
        resp = self.http.get(URL, timeout=60)
        resp.raise_for_status()
        resp.encoding = "utf-8"  # o servidor declara ISO-8859-1, mas envia UTF-8
        verificar_disponivel(resp.text)
        self.soup = BeautifulSoup(resp.text, "html.parser")
        form = self.soup.find("form", id=FORM)
        if form is None:
            raise RuntimeError("A página de pesquisa veio sem o formulário (o site mudou ou está com problema).")
        for el in form.find_all(["input", "select", "textarea"]):
            nome = el.get("name")
            if not nome:
                continue
            tipo = (el.get("type") or "").lower()
            if el.name == "select":
                opt = el.find("option", selected=True) or el.find("option")
                self.campos[nome] = opt["value"] if opt else ""
            elif tipo in ("button", "submit", "image"):
                continue
            elif tipo in ("radio", "checkbox"):
                if el.has_attr("checked"):
                    self.campos[nome] = el.get("value", "on")
            else:
                self.campos[nome] = el.get("value", "") if el.name == "input" else el.get_text()
        viewstate = form.find("input", attrs={"name": "javax.faces.ViewState"})
        self.campos["javax.faces.ViewState"] = viewstate["value"]

    def ajax(self, extras: dict[str, str]) -> BeautifulSoup:
        dados = {"AJAXREQUEST": "_viewRoot", **self.campos, **extras}
        resp = self.http.post(URL, data=dados, timeout=60)
        resp.raise_for_status()
        verificar_disponivel(resp.text)
        time.sleep(self.pausa)
        soup = BeautifulSoup(resp.text, "html.parser")
        vs = soup.find("input", attrs={"name": "javax.faces.ViewState"})
        if vs and vs.get("value"):
            self.campos["javax.faces.ViewState"] = vs["value"]
        return soup

    def _select(self, soup: BeautifulSoup, chave: str):
        sel = soup.find("select", id=f"{FORM}:{SELECT_IDS[chave]}")
        if sel is None:
            raise RuntimeError(f"Campo '{chave}' não encontrado na resposta.")
        return sel

    # ---------- preenchimento dos filtros ----------

    def escolher(self, soup: BeautifulSoup, chave: str, rotulo: str, disparar: bool) -> BeautifulSoup:
        """Seleciona a opção pelo texto; se o select tem onchange AJAX, dispara-o."""
        sel = self._select(soup, chave)
        alvo = normalizar(rotulo)
        opcoes = [(o["value"], o.get_text(strip=True)) for o in sel.find_all("option")]
        valor = next((v for v, t in opcoes if normalizar(t) == alvo), None)
        if valor is None:
            disponiveis = ", ".join(t for _, t in opcoes if t != "Selecione")
            raise RuntimeError(f"Opção '{rotulo}' não existe em '{chave}'. Disponíveis: {disponiveis}")
        self.campos[sel["name"]] = valor
        if disparar and sel.get("onchange"):
            return self.ajax(parametros_ajax(sel["onchange"]))
        return soup

    def escolher_regime(self, rotulo: str) -> BeautifulSoup:
        alvo = normalizar(rotulo)
        for label in self.soup.find_all("label"):
            if normalizar(label.get_text()) == alvo and label.get("for"):
                radio = self.soup.find("input", id=label["for"])
                self.campos[radio["name"]] = radio["value"]
                return self.ajax(parametros_ajax(radio["onchange"]))
        raise RuntimeError(f"Regime '{rotulo}' não encontrado.")

    def aplicar_filtros(self, f: dict[str, str]) -> None:
        resp = self.escolher(self.soup, "natureza", f["natureza"], disparar=True)
        self.escolher(resp, "tipo", f["tipo"], disparar=False)
        self.escolher_regime(f["regime"])
        # A lista de sistemáticas é a da página inicial (o regime só filtra no servidor).
        resp = self.escolher(self.soup, "sistematica", f["sistematica"], disparar=True)
        self.escolher(resp, "forma", f["forma"], disparar=False)
        self.escolher(self.soup, "status", f["status"], disparar=False)
        self.escolher(self.soup, "microrregiao", f["microrregiao"], disparar=False)

    # ---------- resultados ----------

    @staticmethod
    def extrair_linhas(soup: BeautifulSoup) -> list[dict[str, str]]:
        tabela = soup.find("table", id=f"{FORM}:pagedDataTable")
        if tabela is None:
            return []
        cabecalhos = [" ".join(th.get_text(" ").split()) for th in tabela.select("thead th")]
        linhas = []
        for tr in tabela.select("tbody tr.rich-table-row"):
            for dica in tr.select("span.rich-tool-tip"):  # tooltip repete o texto da célula
                dica.decompose()
            celulas = tr.find_all("td", recursive=False)
            registro = {}
            for nome, td in zip(cabecalhos, celulas):
                if nome:
                    registro[nome] = td.get_text(" ", strip=True)
            m = re.search(r"pagedDataTable:(\d+):", celulas[0].get("id", "")) if celulas else None
            registro["id_interno"] = m.group(1) if m else ""
            linhas.append(registro)
        return linhas

    @staticmethod
    def tem_proxima(soup: BeautifulSoup) -> bool:
        scroller = soup.find("table", id=f"{FORM}:datascrollerSuperior_table")
        if scroller is None:
            return False
        for td in scroller.find_all("td"):
            if td.get_text(strip=True) == "»":
                return "rich-datascr-button-dsbld" not in td.get("class", [])
        return False

    def pesquisar(self, max_paginas: int = 200) -> list[dict[str, str]]:
        botao = self.soup.find("input", id=f"{FORM}:pesquisar")
        resp = self.ajax(parametros_ajax(botao["onclick"]))
        resultados: list[dict[str, str]] = []
        vistos: set[str] = set()

        def acumular(linhas: list[dict[str, str]]) -> int:
            # O site às vezes repete a mesma publicação em linhas diferentes.
            novas = 0
            for r in linhas:
                if r["id_interno"] not in vistos:
                    vistos.add(r["id_interno"])
                    resultados.append(r)
                    novas += 1
            return novas

        acumular(self.extrair_linhas(resp))
        pagina = 1
        while self.tem_proxima(resp) and pagina < max_paginas:
            pagina += 1
            resp = self.ajax({
                "ajaxSingle": f"{FORM}:datascrollerSuperior",
                f"{FORM}:datascrollerSuperior": "next",
            })
            linhas = self.extrair_linhas(resp)
            if not linhas:
                break
            novas = acumular(linhas)
            print(f"  página {pagina}: +{novas} registros", file=sys.stderr)
        return resultados


def salvar(resultados: list[dict[str, str]], saida: Path) -> None:
    saida.parent.mkdir(parents=True, exist_ok=True)
    if saida.suffix.lower() == ".json":
        saida.write_text(json.dumps(resultados, ensure_ascii=False, indent=2), encoding="utf-8")
        return
    colunas = list(dict.fromkeys(k for r in resultados for k in r)) or ["sem resultados"]
    with saida.open("w", newline="", encoding="utf-8-sig") as f:  # utf-8-sig: abre certo no Excel
        w = csv.DictWriter(f, fieldnames=colunas, delimiter=";")
        w.writeheader()
        w.writerows(resultados)


def main() -> None:
    ap = argparse.ArgumentParser(description="Busca publicações no Licita-Web (SEFAZ-CE).")
    ap.add_argument("-o", "--saida", default="resultados.csv", help="arquivo .csv ou .json")
    ap.add_argument(
        "-s", "--status", nargs="+", default=[FILTROS["status"]],
        help='um ou mais status, ex.: -s "Publicada" "Recebendo propostas"',
    )
    args = ap.parse_args()

    resultados = []
    for status in args.status:
        site = LicitaWeb()
        print(f"Pesquisando status '{status}'...", file=sys.stderr)
        site.abrir()
        site.aplicar_filtros({**FILTROS, "status": status})
        resultados.extend(site.pesquisar())

    for r in resultados:
        print(" | ".join(v for k, v in r.items() if k != "id_interno"))
    salvar(resultados, Path(args.saida))
    print(f"\n{len(resultados)} publicação(ões) encontrada(s). Salvo em {args.saida}", file=sys.stderr)


if __name__ == "__main__":
    main()
