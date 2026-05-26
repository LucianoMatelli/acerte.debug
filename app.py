# app.py — versão com correções de comunicação PNCP

# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import io
import re
import json
import time
import base64
import hashlib
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests
import streamlit as st

# ==========================

# Configuração de página

# ==========================

st.set_page_config(
page_title="Acerte Licitações - O seu Buscador de Editais",
page_icon="📑",
layout="wide",
)

# ==========================

# Constantes

# ==========================

BASE_DIR = os.path.dirname(**file**)
DATA_DIR = os.path.join(BASE_DIR, "data")

CSV_PNCP_PATHS = [
os.path.join(DATA_DIR, "ListaMunicipiosPNCP.csv"),
"ListaMunicipiosPNCP.csv",
]

ORIGIN = "[https://pncp.gov.br](https://pncp.gov.br)"
BASE_API = "[https://pncp.gov.br/api/search/](https://pncp.gov.br/api/search/)"

HEADERS = {
"User-Agent": (
"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
"AppleWebKit/537.36 (KHTML, like Gecko) "
"Chrome/124.0 Safari/537.36"
),
"Accept": "application/json, text/plain, */*",
"Accept-Language": "pt-BR,pt;q=0.9",
"Referer": "[https://pncp.gov.br/app/editais](https://pncp.gov.br/app/editais)",
"Origin": "[https://pncp.gov.br](https://pncp.gov.br)",
}

TAM_PAGINA_FIXO = 50

STATUS_LABELS = [
"A Receber/Recebendo Proposta",
"Em Julgamento/Propostas Encerradas",
"Encerradas",
"Todos",
]

STATUS_MAP = {
"A Receber/Recebendo Proposta": "recebendo_proposta",
"Em Julgamento/Propostas Encerradas": "julgamento",
"Encerradas": "encerrado",
"Todos": "",
}

# ==========================

# Helpers

# ==========================

def _norm(txt: str) -> str:
if not txt:
return ""

```
import unicodedata

txt = unicodedata.normalize("NFKD", str(txt))
txt = txt.encode("ASCII", "ignore").decode("ASCII")
txt = txt.lower().strip()
return re.sub(r"\s+", " ", txt)
```

def _fmt_dt_iso_to_br(valor: str) -> str:
if not valor:
return ""

```
try:
    valor = valor.replace("Z", "")
    dt = datetime.fromisoformat(valor[:19])
    return dt.strftime("%d/%m/%Y %H:%M")
except Exception:
    return valor
```

def _items_from_json(js) -> List[Dict]:
if isinstance(js, list):
return js

```
if not isinstance(js, dict):
    return []

for key in [
    "items",
    "results",
    "content",
    "data",
    "resultados",
    "licitacoes",
]:
    val = js.get(key)

    if isinstance(val, list):
        return val

    if isinstance(val, dict):
        for sub in ["items", "content", "data"]:
            subval = val.get(sub)
            if isinstance(subval, list):
                return subval

return []
```

def _build_pncp_link(item: Dict) -> str:
cnpj = str(item.get("orgao_cnpj") or item.get("cnpj") or "").strip()
ano = str(item.get("anoCompra") or item.get("ano") or "").strip()
seq = str(item.get("sequencialCompra") or item.get("numero_sequencial") or "").strip()

```
if cnpj and ano and seq:
    return f"https://pncp.gov.br/app/editais/{cnpj}/{ano}/{seq}"

url = item.get("linkSistemaOrigem") or item.get("url") or ""

if url.startswith("http"):
    return url

return ""
```

# ==========================

# Loader CSV

# ==========================

@st.cache_data(show_spinner=False)
def load_municipios_pncp() -> pd.DataFrame:
encodings = ["utf-8", "utf-8-sig", "latin1", "cp1252"]

```
for path in CSV_PNCP_PATHS:
    if not os.path.exists(path):
        continue

    for enc in encodings:
        try:
            df = pd.read_csv(
                path,
                sep=None,
                engine="python",
                dtype=str,
                encoding=enc,
                on_bad_lines="skip",
            )

            cols = {_norm(c): c for c in df.columns}

            col_nome = (
                cols.get("municipio")
                or cols.get("nome")
                or cols.get("municipios")
            )

            col_codigo = (
                cols.get("id")
                or cols.get("codigo")
                or cols.get("codigo_pncp")
            )

            col_uf = cols.get("uf")

            if not col_nome or not col_codigo:
                continue

            out = pd.DataFrame({
                "nome": df[col_nome].astype(str).str.strip(),
                "codigo_pncp": df[col_codigo].astype(str).str.strip(),
            })

            if col_uf:
                out["uf"] = df[col_uf].astype(str).str.strip()
            else:
                out["uf"] = ""

            out["nome_norm"] = out["nome"].map(_norm)

            out = out.drop_duplicates(subset=["codigo_pncp"])

            return out.reset_index(drop=True)

        except Exception:
            continue

raise Exception("Não foi possível carregar ListaMunicipiosPNCP.csv")
```

# ==========================

# CONSULTA PNCP CORRIGIDA

# ==========================

def consultar_pncp_por_municipio(
municipio_id: str,
status_value: str = "",
tam_pagina: int = TAM_PAGINA_FIXO,
) -> List[Dict]:

```
resultados = []

session = requests.Session()

pagina = 1

while True:

    params = {
        "pagina": pagina,
        "tamanhoPagina": tam_pagina,
        "codigoMunicipioIbge": municipio_id,
        "ordenacao": "-data",
    }

    if status_value:
        params["status"] = status_value

    try:
        r = session.get(
            BASE_API,
            params=params,
            headers=HEADERS,
            timeout=30,
        )

        if r.status_code != 200:
            break

        js = r.json()

        itens = _items_from_json(js)

        # fallback automático
        if not itens:

            fallback_url = "https://pncp.gov.br/api/consulta/v1/contratacoes/publicacao"

            r2 = session.get(
                fallback_url,
                params={
                    "codigoMunicipioIbge": municipio_id,
                    "pagina": pagina,
                    "tamanhoPagina": tam_pagina,
                },
                headers=HEADERS,
                timeout=30,
            )

            if r2.status_code == 200:
                itens = _items_from_json(r2.json())

        if not itens:
            break

        resultados.extend(itens)

        if len(itens) < tam_pagina:
            break

        pagina += 1

        time.sleep(0.08)

    except Exception as e:
        print("ERRO PNCP:", e)
        break

return resultados
```

# ==========================

# Montagem de registro

# ==========================

def montar_registro(item: Dict, municipio_codigo: str) -> Dict:

```
titulo = (
    item.get("objetoCompra")
    or item.get("titulo")
    or item.get("objeto")
    or ""
)

cidade = (
    item.get("municipioNome")
    or item.get("municipio_nome")
    or ""
)

uf = (
    item.get("ufSigla")
    or item.get("uf")
    or ""
)

orgao = (
    item.get("orgaoEntidade", {}).get("razaoSocial")
    or item.get("orgao_nome")
    or ""
)

modalidade = (
    item.get("modalidadeNome")
    or item.get("modalidade_licitacao_nome")
    or ""
)

pub = (
    item.get("dataPublicacaoPncp")
    or item.get("data_publicacao_pncp")
    or ""
)

fim = (
    item.get("dataEncerramentoProposta")
    or item.get("fimRecebimentoProposta")
    or ""
)

processo = (
    item.get("numeroControlePNCP")
    or item.get("numeroProcesso")
    or ""
)

return {
    "municipio_codigo": municipio_codigo,
    "Cidade": cidade,
    "UF": uf,
    "Título": titulo,
    "Objeto": titulo,
    "Link para o edital": _build_pncp_link(item),
    "Modalidade": modalidade,
    "Orgão": orgao,
    "Publicação": _fmt_dt_iso_to_br(pub),
    "Fim do envio": _fmt_dt_iso_to_br(fim),
    "Processo": processo,
    "_pub_raw": pub,
}
```

# ==========================

# CACHE COLETA

# ==========================

@st.cache_data(ttl=600, show_spinner=False)
def coletar_por_assinatura(signature: dict) -> pd.DataFrame:

```
registros = []

municipios = signature.get("municipios", [])
status = signature.get("status", "")
palavra = (signature.get("q") or "").strip().lower()

for codigo in municipios:

    itens = consultar_pncp_por_municipio(
        municipio_id=codigo,
        status_value=status,
    )

    for item in itens:
        registros.append(montar_registro(item, codigo))

if not registros:
    return pd.DataFrame()

df = pd.DataFrame(registros)

if palavra:
    mask = (
        df["Título"].fillna("").str.lower().str.contains(palavra)
        |
        df["Objeto"].fillna("").str.lower().str.contains(palavra)
    )

    df = df[mask]

try:
    df["_pub_dt"] = pd.to_datetime(df["_pub_raw"], errors="coerce")
    df = df.sort_values("_pub_dt", ascending=False)
except Exception:
    pass

return df.reset_index(drop=True)
```

# ==========================

# Estado sessão

# ==========================

def _ensure_session_state():

```
if "selected_municipios" not in st.session_state:
    st.session_state.selected_municipios = []

if "results_df" not in st.session_state:
    st.session_state.results_df = None

if "card_page" not in st.session_state:
    st.session_state.card_page = 1
```

# ==========================

# MAIN

# ==========================

def main():

```
st.title("📑 Acerte Licitações")

_ensure_session_state()

try:
    pncp_df = load_municipios_pncp()
except Exception as e:
    st.error(f"Erro ao carregar CSV: {e}")
    st.stop()

st.sidebar.header("Filtros")

ufs = sorted([
    x for x in pncp_df["uf"].dropna().unique().tolist()
    if x
])

uf = st.sidebar.selectbox("UF", ["Selecione"] + ufs)

municipios = []

if uf != "Selecione":

    temp = pncp_df[pncp_df["uf"] == uf]

    municipios = sorted(temp["nome"].tolist())

municipio = st.sidebar.selectbox(
    "Município",
    ["Selecione"] + municipios,
)

if st.sidebar.button("Adicionar município"):

    if municipio != "Selecione":

        row = pncp_df[
            pncp_df["nome"] == municipio
        ].iloc[0]

        codigo = row["codigo_pncp"]

        ja = [x["codigo_pncp"] for x in st.session_state.selected_municipios]

        if codigo not in ja:
            st.session_state.selected_municipios.append({
                "codigo_pncp": codigo,
                "nome": municipio,
                "uf": uf,
            })

st.sidebar.markdown("---")

st.sidebar.markdown("### Municípios selecionados")

for m in st.session_state.selected_municipios:
    st.sidebar.write(f"• {m['nome']} / {m['uf']}")

palavra = st.sidebar.text_input("Palavra-chave")

status_label = st.sidebar.radio(
    "Status",
    STATUS_LABELS,
)

pesquisar = st.sidebar.button(
    "🔎 Pesquisar",
    use_container_width=True,
)

if not pesquisar:
    st.info("Configure os filtros e clique em Pesquisar.")
    return

if not st.session_state.selected_municipios:
    st.warning("Selecione ao menos um município.")
    return

signature = {
    "municipios": [
        x["codigo_pncp"]
        for x in st.session_state.selected_municipios
    ],
    "status": STATUS_MAP.get(status_label, ""),
    "q": palavra,
}

with st.spinner("Consultando PNCP..."):
    df = coletar_por_assinatura(signature)

if df.empty:
    st.warning("Nenhum edital encontrado.")
    return

st.success(f"{len(df)} editais encontrados")

for _, row in df.iterrows():

    st.markdown(f"""
    ### {row['Título']}

    **Cidade:** {row['Cidade']} / {row['UF']}  
    **Órgão:** {row['Orgão']}  
    **Modalidade:** {row['Modalidade']}  
    **Publicação:** {row['Publicação']}  
    **Fim envio:** {row['Fim do envio']}  
    **Processo:** {row['Processo']}  

    [Abrir edital]({row['Link para o edital']})

    ---
    """)

# EXPORTAÇÃO
xlsx = io.BytesIO()

with pd.ExcelWriter(xlsx, engine="openpyxl") as writer:
    df.to_excel(writer, index=False)

st.download_button(
    "⬇️ Baixar XLSX",
    data=xlsx.getvalue(),
    file_name=f"pncp_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    use_container_width=True,
)
```

if **name** == "**main**":
main()

