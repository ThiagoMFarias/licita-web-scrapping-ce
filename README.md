# licita_ce

Web scraping das cotações eletrônicas do Licita-Web da SEFAZ-CE
(https://s2gpr.sefaz.ce.gov.br/licita-web/paginas/licita/PublicacaoList.seam).

O projeto tem dois scripts:

| Script | Para quê | Velocidade |
|---|---|---|
| `scraper.py` | Lista as cotações **abertas agora** (só a tabela de resultados) | segundos |
| `finalizadas.py` | Entra em cada cotação **finalizada** e coleta vencedor, lances e itens com marca | ~15 s por cotação |

## Filtros

Definidos no dicionário `FILTROS` em `scraper.py` e usados pelos dois scripts:

| Campo | Valor |
|---|---|
| Natureza da aquisição | MATERIAL DE CONSUMO |
| Tipo de aquisição | MATERIAL DE LIMPEZA E PRODUÇÃO DE HIGIENIZAÇÃO |
| Regime | Lei 14.133/21 |
| Sistemática | DISPENSA |
| Forma | COTAÇÃO ELETRÔNICA |
| Status | Publicada (`scraper.py`) / Finalizada (`finalizadas.py`) |
| Microrregião | Fortaleza |

Os valores devem ser escritos como aparecem no site (acentos e maiúsculas não
importam). Se um valor não existir, o erro lista as opções disponíveis.

## Instalação

Com [uv](https://docs.astral.sh/uv/) (instala Python e dependências sozinho):

```
uv sync
```

Ou com Python 3.10+ e pip:

```
pip install -r requirements.txt
```

## Cotações abertas: `scraper.py`

```
python scraper.py
python scraper.py -s "Publicada" "Recebendo propostas"
python scraper.py -o resultados.json
```

Mostra no terminal e salva em `resultados.csv` (separado por `;`, abre direto no
Excel) ou `.json`.

"Publicada" é um status curto: quando o acolhimento de propostas começa, a
publicação passa a "Recebendo propostas". Use `-s` com os dois para não perder nada.

## Cotações finalizadas: `finalizadas.py`

```
python finalizadas.py -n 5          # só as 5 primeiras (teste)
python finalizadas.py               # todas
python finalizadas.py -o saida/2026 # gera saida/2026.csv e saida/2026.json
python finalizadas.py --recomecar   # ignora o que já foi coletado e começa do zero
```

Para cada cotação, faz o mesmo que um usuário no site: clica na linha →
"Visualizar" → "(expandir)" em cada grupo. Nas cotações **julgadas por item**
(sem grupos), a marca não aparece na página da cotação; ela é lida em
"Visualizar Propostas" de cada item.

### Saída

- **`finalizadas.csv`**: uma linha por item, separado por `;`, números com vírgula decimal.
  Colunas: publicação, nº do processo, contratante, início do acolhimento, abertura
  das propostas, total estimado da cotação, grupo, fornecedor vencedor e total do
  lance (do grupo), e por item: nº, descrição completa, unidade de fornecimento,
  quantidade, valor e total estimado, valor e total do lance, marca, fornecedor e status.
- **`finalizadas.json`**: a mesma informação aninhada (cotação → grupos → itens).

Itens fracassados ou desertos aparecem com a marca vazia e o status do site.

### Retomada

Cada cotação é gravada na hora em `finalizadas.parcial.jsonl`. Se a coleta parar
(erro, Ctrl+C, terminal fechado, queda de rede), é só rodar o mesmo comando de novo:
ele pula as já coletadas e baixa só o que falta. Quando a coleta termina sem
falhas, esse arquivo é apagado.

Execuções seguintes também reaproveitam o `finalizadas.json` existente, então
rodar de novo dias depois baixa só as cotações finalizadas novas.

## Painel de análise (BI): `bi.py`

```
streamlit run bi.py
```

Abre no navegador (http://localhost:8501) um painel interativo sobre o `finalizadas.csv`
(somente leitura; o arquivo não é alterado). Os filtros da barra lateral (período, busca,
produto, especificação, marca, fornecedor, órgão) valem para todas as abas:

| Aba | Responde |
|---|---|
| 🏆 Fornecedores | Quem mais ganhou (cotações, itens, valor), desconto médio, marcas e produtos de cada um |
| 📦 Itens | O que mais saiu (vezes, quantidade, valor), com preço médio/mín./máx. |
| 🏷️ Marcas | Marcas mais usadas e, para uma marca, em que produtos aparece, quem ofertou e a que preço |
| 🔗 Relações | Cruzamento de dois campos quaisquer (ex.: Fornecedor × Marca, Produto × Marca, Órgão × Fornecedor) |
| 📈 Preços | Preço de um item ao longo do tempo e comparação de preço entre marcas |
| 📄 Dados | Tabela filtrada, com download em CSV |

Itens podem ser vistos em três níveis:

- **Produto**: só o nome (`SACO PARA LIXO`), junta todos os tamanhos;
- **Especificação**: produto + capacidade + cor + embalagem
  (`SACO PARA LIXO · 100 L · PRETO · PACOTE 100 UN`), o nível certo para comparar preço;
- **Descrição completa**: o texto exato do catálogo.

Marcas escritas de formas diferentes (`Absolut`, `ABSOLUT`) são unificadas.

## Comportamento do site (e como o script lida com ele)

- **Horário de funcionamento:** fora do horário, o site responde "Serviço
  indisponível neste horário". O script detecta isso, para e salva o que já tem.
- **Queda de rede/VPN/DNS:** cada requisição é repetida algumas vezes; se a conexão
  continuar fora, o script espera e tenta a mesma cotação de novo por alguns
  minutos antes de parar.
- **Voltar para a lista reabre sempre a primeira cotação vista** (estado guardado
  no servidor). Por isso cada cotação é aberta numa pesquisa nova, com os filtros
  reaplicados e salto direto para a página dela. O script confere se o nº do
  processo aberto é o da linha clicada.
- **Linhas repetidas na lista:** o site às vezes mostra a mesma cotação em várias
  linhas; o script remove as repetições.
- **Codificação:** o site declara ISO-8859-1 mas envia UTF-8 em algumas páginas;
  o script trata os dois casos.

## Boas práticas

O script faz uma requisição por vez, com pausa de 0,5 s entre elas. Não rode
várias cópias ao mesmo tempo. Para reduzir ainda mais a carga, aumente a pausa:
`ColetorFinalizadas(..., pausa=1.5)` em `finalizadas.py`.

## Observações técnicas

- O pacote `truststore` faz o Python usar os certificados do sistema operacional,
  necessário em redes com proxy corporativo.
- Os campos do formulário são localizados pelo texto dos rótulos e os parâmetros
  AJAX são lidos do próprio HTML, porque os IDs `j_idNNN` do JSF são gerados
  automaticamente e podem mudar.
