# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import io
import re
import json
import time
import base64
import unicodedata
import hashlib
from datetime import datetime, timedelta
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
# Constantes e caminhos
# ==========================
BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(BASE_DIR, "data")
CSV_PNCP_PATHS = [
    os.path.join(DATA_DIR, "ListaMunicipiosPNCP.csv"),
    "ListaMunicipiosPNCP.csv",
]
CSV_IBGE_PATHS = [
    os.path.join(DATA_DIR, "IBGE_Municipios.csv"),
    "IBGE_Municipios.csv",
]
SAVED_SEARCHES_PATH = os.path.join(BASE_DIR, "saved_searches.json")
SAVED_TR_PATH = os.path.join(BASE_DIR, "tr_marks.json")
SAVED_NA_PATH = os.path.join(BASE_DIR, "na_marks.json")

ORIGIN = "https://pncp.gov.br"
BASE_API_CONSULTA = ORIGIN + "/api/consulta/v1"
BASE_API_CONSULTA_PUBLICACAO = BASE_API_CONSULTA + "/contratacoes/publicacao"
BASE_API_CONSULTA_PROPOSTA = BASE_API_CONSULTA + "/contratacoes/proposta"
BASE_API_ORGAOS = ORIGIN + "/pncp-api/v1/orgaos/"
BASE_API_ARQUIVOS = ORIGIN + "/pncp-api/v1/orgaos/{cnpj}/compras/{ano}/{seq}/arquivos"
BASE_API_ITENS = ORIGIN + "/pncp-api/v1/orgaos/{cnpj}/compras/{ano}/{seq}/itens"
BASE_API_MODALIDADES = ORIGIN + "/pncp-api/v1/modalidades"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://pncp.gov.br/app/editais",
    "Accept-Language": "pt-BR,pt;q=0.9",
    "Accept": "application/json, text/plain, */*",
}
TAM_PAGINA_FIXO = 100
CONSULTA_TAM_PAGINA = 100
CONSULTA_MAX_PAGINAS_MODALIDADE = 3
CONSULTA_DIAS_PUBLICACAO = 365
CONSULTA_TIMEOUT_HTTP = 12
CONSULTA_MAX_REQUISICOES_MUNICIPIO = 120
CONSULTA_MAX_SEGUNDOS_MUNICIPIO = 40
CONSULTA_MAX_ERROS_CONSECUTIVOS = 4
ANOS_LOOKBACK = 1
MAX_SEQ_SCAN = 180
MISS_STREAK_STOP = 40
MAX_ORGAOS_POR_MUNICIPIO = 8
MODALIDADES_PADRAO = list(range(1, 14))

STATUS_LABELS = [
    "A Receber/Recebendo Proposta",
    "Em Julgamento/Propostas Encerradas",
    "Encerradas",
    "Todos",
]
STATUS_MAP = {
    "A Receber/Recebendo Proposta": "recebendo_proposta",
    "Em Julgamento/Propostas Encerradas": "em_julgamento",
    "Encerradas": "encerrado",
    "Todos": "",
}
UF_PLACEHOLDER = "— Selecione a UF —"

# ==========================
# Utilitários
# ==========================
def _secret_int(name: str, default: int) -> int:
    try:
        v = int(st.secrets.get(name, default))
        return v if v > 0 else default
    except Exception:
        return default


CFG_ANOS_LOOKBACK = _secret_int("PNCP_ANOS_LOOKBACK", ANOS_LOOKBACK)
CFG_MAX_SEQ_SCAN = _secret_int("PNCP_MAX_SEQ_SCAN", MAX_SEQ_SCAN)
CFG_MISS_STREAK_STOP = _secret_int("PNCP_MISS_STREAK_STOP", MISS_STREAK_STOP)
CFG_MAX_ORGAOS_POR_MUNICIPIO = _secret_int("PNCP_MAX_ORGAOS_POR_MUNICIPIO", MAX_ORGAOS_POR_MUNICIPIO)
CFG_CONSULTA_TAM_PAGINA = min(500, _secret_int("PNCP_CONSULTA_TAM_PAGINA", CONSULTA_TAM_PAGINA))
CFG_CONSULTA_MAX_PAGINAS_MODALIDADE = _secret_int("PNCP_CONSULTA_MAX_PAGINAS_MODALIDADE", CONSULTA_MAX_PAGINAS_MODALIDADE)
CFG_CONSULTA_DIAS_PUBLICACAO = _secret_int("PNCP_CONSULTA_DIAS_PUBLICACAO", CONSULTA_DIAS_PUBLICACAO)
CFG_CONSULTA_TIMEOUT_HTTP = _secret_int("PNCP_CONSULTA_TIMEOUT_HTTP", CONSULTA_TIMEOUT_HTTP)
CFG_CONSULTA_MAX_REQUISICOES_MUNICIPIO = _secret_int(
    "PNCP_CONSULTA_MAX_REQUISICOES_MUNICIPIO",
    CONSULTA_MAX_REQUISICOES_MUNICIPIO,
)
CFG_CONSULTA_MAX_SEGUNDOS_MUNICIPIO = _secret_int(
    "PNCP_CONSULTA_MAX_SEGUNDOS_MUNICIPIO",
    CONSULTA_MAX_SEGUNDOS_MUNICIPIO,
)
CFG_CONSULTA_MAX_ERROS_CONSECUTIVOS = _secret_int(
    "PNCP_CONSULTA_MAX_ERROS_CONSECUTIVOS",
    CONSULTA_MAX_ERROS_CONSECUTIVOS,
)
CFG_ENABLE_FALLBACK_SCAN = str(st.secrets.get("PNCP_ENABLE_FALLBACK_SCAN", "false")).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _norm(s: str) -> str:
    s = str(s or "").strip().lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")

def _items_from_json(js) -> List[Dict]:
    if isinstance(js, dict):
        for k in ["items", "results", "conteudo", "licitacoes", "data", "documents", "documentos", "content", "resultados"]:
            v = js.get(k)
            if isinstance(v, list):
                return v
    if isinstance(js, list):
        return js
    return []

def _fmt_dt_iso_to_br(dt: str) -> str:
    if not dt:
        return ""
    try:
        ts = pd.to_datetime(dt, errors="coerce", utc=False)
        if pd.isna(ts):
            return ""
        return ts.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return ""

def _full_url(item_url: str) -> str:
    if not item_url:
        return ""
    if isinstance(item_url, str) and item_url.startswith("http"):
        return item_url
    return ORIGIN.rstrip("/") + "/" + str(item_url).lstrip("/")

def _build_pncp_link(item: Dict) -> str:
    cnpj = str(item.get("orgao_cnpj", "") or "").strip()
    ano = str(item.get("ano", "") or "").strip()
    seq = str(item.get("numero_sequencial", "") or "").strip()
    if len(cnpj) == 14 and ano.isdigit() and seq:
        return f"{ORIGIN}/app/editais/{cnpj}/{ano}/{seq}"
    raw = item.get("item_url", "") or item.get("url", "") or ""
    url = _full_url(raw)
    url = url.replace("/app/compras/", "/app/editais/").replace("/compras/", "/app/editais/")
    return url

def _primeiro_valor(*args):
    for a in args:
        if a:
            return a
    return ""

def _uid_from_row(row: Dict) -> str:
    cnpj = str(row.get("_orgao_cnpj") or "").strip()
    ano = str(row.get("_ano") or "").strip()
    seq = str(row.get("_seq") or "").strip()
    if len(cnpj) == 14 and ano.isdigit() and seq:
        return f"{cnpj}-{ano}-{seq}"
    link = str(row.get("Link para o edital") or "")
    m = re.search(r"/app/editais/(\d{14})/(\d{4})/(\w+)", link)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    base = f"{row.get('Título','')}-{row.get('municipio_codigo','')}-{row.get('_pub_raw','')}-{row.get('Orgão','')}"
    return hashlib.md5(base.encode("utf-8")).hexdigest()

# ==========================
# Persistência via GitHub Contents API
# ==========================
def _gh_headers() -> Dict[str, str]:
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "AcerteLicitacoes/PNCP",
    }
    tok = st.secrets.get("GITHUB_TOKEN")
    if tok:
        h["Authorization"] = f"token {tok}"
    return h

def _gh_cfg_ok() -> bool:
    has_repo = bool(st.secrets.get("GITHUB_REPO_TEST") or st.secrets.get("GITHUB_REPO"))
    return has_repo

def _gh_paths(filename: str) -> Tuple[str, str, str]:
    repo = st.secrets.get("GITHUB_REPO_TEST") or st.secrets.get("GITHUB_REPO")
    branch = st.secrets.get("GITHUB_BRANCH_TEST") or st.secrets.get("GITHUB_BRANCH", "main")
    based_test = st.secrets.get("GITHUB_BASEDIR_TEST")
    if based_test is not None:
        based = str(based_test).strip()
    else:
        based = str(st.secrets.get("GITHUB_BASEDIR", "data")).strip()
    path = f"{based.rstrip('/')}/{filename}" if based else filename
    return repo, branch, path

def _gh_get_json(filename: str) -> Tuple[Optional[dict], Optional[str]]:
    if not _gh_cfg_ok():
        return None, None
    repo, branch, path = _gh_paths(filename)
    url = f"https://api.github.com/repos/{repo}/contents/{path}"

    try:
        r = requests.get(url, params={"ref": branch}, headers=_gh_headers(), timeout=30)
    except Exception:
        return None, None

    if r.status_code == 404:
        return None, None
    if r.status_code in (401, 403):
        return None, None
    if r.status_code >= 400:
        return None, None

    try:
        js = r.json()
        content_b64 = js.get("content", "")
        sha = js.get("sha")
        raw = base64.b64decode(content_b64).decode("utf-8")
        return json.loads(raw), sha
    except Exception:
        return None, None

def _gh_put_json(filename: str, payload: dict, sha: Optional[str]) -> None:
    if not _gh_cfg_ok():
        raise RuntimeError("GitHub não configurado")
    if not st.secrets.get("GITHUB_TOKEN"):
        raise RuntimeError("GITHUB_TOKEN ausente para escrita no GitHub")

    repo, branch, path = _gh_paths(filename)
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    content_b64 = base64.b64encode(
        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    ).decode("utf-8")

    data = {
        "message": f"chore: atualizar {path} via app",
        "content": content_b64,
        "branch": branch,
        "committer": {
            "name": st.secrets.get("GITHUB_COMMITTER_NAME", "PNCP Bot"),
            "email": st.secrets.get("GITHUB_COMMITTER_EMAIL", "bot@acertelicitacoes.local"),
        },
    }
    if sha:
        data["sha"] = sha

    r = requests.put(url, headers=_gh_headers(), json=data, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"GitHub PUT {r.status_code}: {r.text[:180]}")

# ==========================
# Loaders locais + GitHub (híbridos)
# ==========================
@st.cache_data(show_spinner=False)
def load_municipios_pncp() -> pd.DataFrame:
    encodings = ["utf-8", "utf-8-sig", "latin1", "cp1252"]
    seps = [",", ";", "\t", "|"]
    last_err = None

    def _guess_columns(df: pd.DataFrame):
        cols_norm = {_norm(c): c for c in df.columns}
        col_nome = cols_norm.get("municipio") or cols_norm.get("nome") or ("Municipio" if "Municipio" in df.columns else None)
        col_codigo = cols_norm.get("id") or cols_norm.get("codigo") or ("id" if "id" in df.columns else None)
        col_uf = cols_norm.get("uf") or cols_norm.get("estado") or None
        col_ibge = (
            cols_norm.get("codigo_ibge")
            or cols_norm.get("cod_ibge")
            or cols_norm.get("ibge")
            or cols_norm.get("municipio_ibge")
            or cols_norm.get("codigo_municipio_ibge")
        )
        return col_nome, col_codigo, col_uf, col_ibge

    for path in CSV_PNCP_PATHS:
        if os.path.exists(path):
            for enc in encodings:
                for sep in seps:
                    try:
                        df = pd.read_csv(path, dtype=str, sep=sep, encoding=enc, engine="python", on_bad_lines="skip")
                        if df is None or df.shape[0] == 0 or df.shape[1] == 0:
                            continue
                        col_nome, col_codigo, col_uf, col_ibge = _guess_columns(df)
                        if not col_nome or not col_codigo:
                            raise ValueError("Não foi possível detectar colunas de 'Municipio' e 'id'.")
                        out = pd.DataFrame({
                            "nome": df[col_nome].astype(str).str.strip(),
                            "codigo_pncp": df[col_codigo].astype(str).str.strip(),
                        })
                        out["uf"] = df[col_uf].astype(str).str.strip() if col_uf else ""
                        out["codigo_ibge"] = df[col_ibge].astype(str).str.strip() if col_ibge else ""
                        out["nome_norm"] = out["nome"].map(_norm)
                        out = out[out["codigo_pncp"] != ""].drop_duplicates(subset=["codigo_pncp"]).reset_index(drop=True)
                        return out
                    except Exception as e:
                        last_err = e
                        continue
    if last_err:
        raise last_err
    raise FileNotFoundError("ListaMunicipiosPNCP.csv não encontrada em ./data ou na raiz do projeto.")

@st.cache_data(show_spinner=False)
def load_ibge_catalog() -> Optional[pd.DataFrame]:
    encodings = ["utf-8", "utf-8-sig", "latin1", "cp1252"]
    seps = [",", ";", "\t", "|"]
    for path in CSV_IBGE_PATHS:
        if os.path.exists(path):
            for enc in encodings:
                for sep in seps:
                    try:
                        df = pd.read_csv(path, dtype=str, sep=sep, encoding=enc, engine="python", on_bad_lines="skip")
                        if df is None or df.shape[0] == 0 or df.shape[1] < 2:
                            continue
                        cols = {c.lower().strip(): c for c in df.columns}
                        col_uf = next((cols[k] for k in cols if k in ["uf", "sigla_uf", "estado"]), None)
                        col_mun = next((cols[k] for k in cols if k in ["municipio", "município", "nome"]), None)
                        col_ibge = next(
                            (
                                cols[k]
                                for k in cols
                                if k in ["codigo_ibge", "cod_ibge", "ibge", "codigo", "id_municipio", "municipio_id"]
                            ),
                            None,
                        )
                        if not col_uf or not col_mun:
                            continue
                        out = pd.DataFrame({
                            "uf": df[col_uf].astype(str).str.strip().str.upper(),
                            "municipio": df[col_mun].astype(str).str.strip(),
                        })
                        out["codigo_ibge"] = df[col_ibge].astype(str).str.strip() if col_ibge else ""
                        out["municipio_norm"] = out["municipio"].map(_norm)
                        out = out.drop_duplicates(subset=["uf", "municipio_norm"]).reset_index(drop=True)
                        return out
                    except Exception:
                        continue
    return None

def _load_saved_searches() -> Dict[str, Dict]:
    js, _ = _gh_get_json("saved_searches.json")
    if isinstance(js, dict):
        return js
    try:
        with open(SAVED_SEARCHES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _persist_saved_searches(d: Dict[str, Dict]):
    try:
        _, sha = _gh_get_json("saved_searches.json")
        _gh_put_json("saved_searches.json", d, sha)
        return
    except Exception as e:
        st.warning(f"Não consegui salvar no GitHub (usando fallback local): {e}")
    try:
        with open(SAVED_SEARCHES_PATH, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception as e:
        st.error(f"Falha ao salvar pesquisas localmente: {e}")

def _load_marks(path: str, remote_name: str) -> Dict[str, bool]:
    js, _ = _gh_get_json(remote_name) if _gh_cfg_ok() else (None, None)
    if isinstance(js, dict):
        return {str(k): bool(v) for k, v in js.items()}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {str(k): bool(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except Exception:
        return {}

def _persist_marks(path: str, remote_name: str, d: Dict[str, bool]):
    try:
        _, sha = _gh_get_json(remote_name)
        _gh_put_json(remote_name, d, sha)
        return
    except Exception as e:
        st.warning(f"Não consegui salvar no GitHub (usando fallback local): {e}")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception as e:
        st.error(f"Falha ao salvar marcações localmente: {e}")

# ==========================
# Coleta PNCP (migrada para endpoints oficiais do manual)
# ==========================
@st.cache_data(ttl=21600, show_spinner=False)
def _listar_modalidades_ids() -> List[int]:
    ids: List[int] = []
    for params in ({}, {"statusAtivo": "true"}):
        try:
            r = requests.get(BASE_API_MODALIDADES, params=params, headers=HEADERS, timeout=30)
            if r.status_code >= 400:
                continue
            js = r.json()
        except Exception:
            continue
        itens = js if isinstance(js, list) else _items_from_json(js)
        for it in itens:
            try:
                v = int(it.get("id") or it.get("codigo") or it.get("modalidadeId"))
                if v > 0:
                    ids.append(v)
            except Exception:
                continue
    ids = sorted({x for x in ids if 1 <= int(x) <= 13})
    return ids if ids else MODALIDADES_PADRAO


def _parse_numero_controle_pncp(numero_controle: str) -> Tuple[str, str, str]:
    n = str(numero_controle or "").strip()
    m = re.search(r"^(\d{14})-1-(\d+)/(\d{4})$", n)
    if not m:
        return "", "", ""
    return m.group(1), m.group(3), m.group(2)


def _status_compativel_consulta(status_value: str, situacao_id: Optional[int], data_encerramento: str) -> bool:
    if not status_value:
        return True

    agora = pd.Timestamp.now(tz=None)
    fim = pd.to_datetime(data_encerramento, errors="coerce", utc=False)
    is_fechada_data = bool(pd.notna(fim) and fim < agora)

    if status_value == "recebendo_proposta":
        # Manual 6.4: propostas em aberto.
        # Regras práticas: situação divulgada (1) e fim de recebimento >= agora.
        if pd.isna(fim):
            return False
        if situacao_id is not None and int(situacao_id) != 1:
            return False
        return bool(fim >= agora)

    if status_value == "em_julgamento":
        # Não existe status explícito "em julgamento" no domínio da contratação.
        # Aproximação: já encerrou proposta, mas contratação segue divulgada (1).
        return situacao_id == 1 and is_fechada_data

    if status_value == "encerrado":
        if situacao_id in {2, 3, 4}:  # revogada/anulada/suspensa
            return True
        return is_fechada_data and situacao_id not in {1}

    return True


def _consulta_get_items(url: str, params: Dict) -> Tuple[List[Dict], Optional[str]]:
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=CFG_CONSULTA_TIMEOUT_HTTP)
    except Exception as e:
        return [], f"request_error:{e}"

    if r.status_code in (204, 404):
        return [], None
    if r.status_code >= 400:
        return [], f"http_{r.status_code}"

    body = (r.text or "").strip()
    ctype = (r.headers.get("content-type") or "").lower()
    if "json" not in ctype and not body.startswith("{") and not body.startswith("["):
        return [], "non_json"

    try:
        js = r.json()
    except Exception as e:
        return [], f"json_error:{e}"

    itens = js if isinstance(js, list) else _items_from_json(js)
    return (itens if isinstance(itens, list) else []), None


def _consultar_pncp_api_consulta(
    municipio_id: str,
    municipio_nome: str,
    municipio_uf: str,
    municipio_ibge: str,
    status_value: str,
) -> List[Dict]:
    modalidades = _listar_modalidades_ids()
    data_final = datetime.now().strftime("%Y%m%d")
    data_inicial = (datetime.now() - timedelta(days=CFG_CONSULTA_DIAS_PUBLICACAO)).strftime("%Y%m%d")
    dt_inicial = pd.Timestamp(datetime.now() - timedelta(days=CFG_CONSULTA_DIAS_PUBLICACAO))

    endpoint = BASE_API_CONSULTA_PUBLICACAO
    endpoint_is_proposta = False
    if status_value == "recebendo_proposta":
        endpoint = BASE_API_CONSULTA_PROPOSTA
        endpoint_is_proposta = True

    mun_norm = _norm(municipio_nome)
    resultados: List[Dict] = []
    vistos = set()
    teve_resposta_consulta = False
    req_count = 0
    erros_consecutivos = 0
    t0 = time.monotonic()
    limite_tempo_atingido = False
    limite_req_atingido = False
    abortar_por_erro = False

    for modalidade_id in modalidades:
        if (time.monotonic() - t0) >= CFG_CONSULTA_MAX_SEGUNDOS_MUNICIPIO:
            limite_tempo_atingido = True
            break
        if req_count >= CFG_CONSULTA_MAX_REQUISICOES_MUNICIPIO:
            limite_req_atingido = True
            break
        for pagina in range(1, CFG_CONSULTA_MAX_PAGINAS_MODALIDADE + 1):
            if (time.monotonic() - t0) >= CFG_CONSULTA_MAX_SEGUNDOS_MUNICIPIO:
                limite_tempo_atingido = True
                break
            if req_count >= CFG_CONSULTA_MAX_REQUISICOES_MUNICIPIO:
                limite_req_atingido = True
                break

            params: Dict[str, str | int] = {
                "codigoModalidadeContratacao": modalidade_id,
                "pagina": pagina,
                "tamanhoPagina": CFG_CONSULTA_TAM_PAGINA,
            }
            if endpoint_is_proposta:
                params["dataFinal"] = data_final
            else:
                params["dataInicial"] = data_inicial
                params["dataFinal"] = data_final
            if municipio_uf:
                params["uf"] = str(municipio_uf).upper()
            if municipio_ibge:
                params["codigoMunicipioIbge"] = str(municipio_ibge)

            req_count += 1
            itens, err = _consulta_get_items(endpoint, params)
            if err:
                erros_consecutivos += 1
                if erros_consecutivos >= CFG_CONSULTA_MAX_ERROS_CONSECUTIVOS:
                    abortar_por_erro = True
                break

            erros_consecutivos = 0
            teve_resposta_consulta = True
            if not itens:
                break

            for item in itens:
                unidade = item.get("unidadeOrgao") or {}
                orgao = item.get("orgaoEntidade") or {}
                cidade_api = str(unidade.get("municipioNome") or municipio_nome or "").strip()
                uf_api = str(unidade.get("ufSigla") or municipio_uf or "").strip().upper()
                ibge_api = str(unidade.get("codigoIbge") or "")

                # Quando não houver código IBGE no catálogo local, filtra por nome/UF.
                if not municipio_ibge:
                    if mun_norm and _norm(cidade_api) != mun_norm:
                        continue
                    if municipio_uf and uf_api and uf_api != str(municipio_uf).upper():
                        continue
                else:
                    if ibge_api and str(ibge_api) != str(municipio_ibge):
                        continue

                sit_id_raw = item.get("situacaoCompraId")
                try:
                    sit_id = int(sit_id_raw) if sit_id_raw is not None else None
                except Exception:
                    sit_id = None
                data_enc = str(item.get("dataEncerramentoProposta") or "")
                data_pub_raw = _primeiro_valor(
                    item.get("dataPublicacaoPncp"),
                    item.get("dataAtualizacao"),
                    item.get("dataInclusao"),
                )
                dt_pub = pd.to_datetime(data_pub_raw, errors="coerce", utc=True)
                if pd.notna(dt_pub):
                    dt_pub = dt_pub.tz_localize(None)

                # Segurança adicional: mantém janela de publicação para todos os status.
                if pd.notna(dt_pub) and dt_pub < dt_inicial:
                    continue

                if not _status_compativel_consulta(status_value, sit_id, data_enc):
                    continue

                numero_controle = str(item.get("numeroControlePNCP") or "")
                cnpj, ano_ctrl, seq_ctrl = _parse_numero_controle_pncp(numero_controle)
                if not cnpj:
                    cnpj = str(orgao.get("cnpj") or "").strip()
                ano = str(item.get("anoCompra") or ano_ctrl or "").strip()
                seq = str(item.get("sequencialCompra") or seq_ctrl or "").strip()

                uid = f"{cnpj}-{ano}-{seq}" if cnpj and ano and seq else numero_controle
                if uid and uid in vistos:
                    continue
                if uid:
                    vistos.add(uid)

                tipo_inst = str(item.get("tipoInstrumentoConvocatorioNome") or "").strip()
                numero_compra = str(item.get("numeroCompra") or "").strip()
                modalidade_nome = str(item.get("modalidadeNome") or "").strip()
                modo_disputa_nome = str(item.get("modoDisputaNome") or "").strip()
                objeto_compra = str(item.get("objetoCompra") or "").strip()
                info_compl = str(item.get("informacaoComplementar") or "").strip()

                if numero_compra and ano:
                    titulo = f"{tipo_inst or 'Contratação'} nº {numero_compra}/{ano}".strip()
                elif numero_compra:
                    titulo = f"{tipo_inst or 'Contratação'} nº {numero_compra}".strip()
                else:
                    titulo = _primeiro_valor(
                        item.get("titulo"),
                        item.get("title"),
                        numero_controle,
                        "Contratação",
                    )

                resultados.append(
                    {
                        "municipio_codigo": municipio_id,
                        "municipio_nome": cidade_api or municipio_nome,
                        "uf": uf_api or municipio_uf,
                        "orgao_cnpj": cnpj,
                        "orgao_nome": str(orgao.get("razaosocial") or orgao.get("razaoSocial") or ""),
                        "ano": ano,
                        "numero_sequencial": seq,
                        "title": titulo,
                        "description": objeto_compra or info_compl,
                        "document_type": tipo_inst,
                        "modalidade_licitacao_nome": modalidade_nome,
                        "tipo_nome": modo_disputa_nome,
                        "unidade_nome": str(unidade.get("nomeUnidade") or ""),
                        "esfera_nome": str(orgao.get("esferaId") or ""),
                        "data_publicacao_pncp": data_pub_raw,
                        "data_fim_vigencia": data_enc,
                        "numeroProcesso": str(item.get("processo") or ""),
                        "item_url": _primeiro_valor(
                            f"/app/editais/{cnpj}/{ano}/{seq}" if cnpj and ano and seq else "",
                            item.get("linkSistemaOrigem"),
                            "",
                        ),
                        "id": uid or numero_controle,
                    }
                )

            if len(itens) < CFG_CONSULTA_TAM_PAGINA:
                break
        if limite_tempo_atingido or limite_req_atingido or abortar_por_erro:
            break

    if abortar_por_erro and not resultados:
        raise RuntimeError("API de Consultas com erro recorrente")

    if not teve_resposta_consulta and not resultados:
        raise RuntimeError("API de Consultas indisponível ou não-JSON")

    return resultados


@st.cache_data(ttl=21600, show_spinner=False)
def _buscar_orgaos_por_municipio_nome(municipio_nome: str) -> List[Dict]:
    nome = str(municipio_nome or "").strip()
    if not nome or len(nome) < 3:
        return []

    coletados: List[Dict] = []
    for pagina in range(1, 6):
        try:
            r = requests.get(
                BASE_API_ORGAOS,
                params={"razaoSocial": nome, "pagina": pagina},
                headers=HEADERS,
                timeout=CFG_CONSULTA_TIMEOUT_HTTP,
            )
            r.raise_for_status()
            js = r.json()
        except Exception:
            break

        itens = js if isinstance(js, list) else _items_from_json(js)
        if not isinstance(itens, list) or not itens:
            break

        coletados.extend(itens)
        if len(itens) < 20:
            break

    mun_norm = _norm(nome)
    ranked: List[Tuple[int, Dict]] = []
    seen = set()
    for o in coletados:
        cnpj = str(o.get("cnpj") or "").strip()
        razao = str(o.get("razaoSocial") or "").strip()
        if len(cnpj) != 14 or cnpj in seen:
            continue
        seen.add(cnpj)

        # reduz ruído de entidades não municipais
        esfera = str(o.get("esferaId") or "").upper()
        if esfera and esfera != "M":
            continue

        rz = _norm(razao)
        if mun_norm and mun_norm not in rz:
            continue

        score = 0
        if f"municipio_de_{mun_norm}" in rz:
            score += 100
        if f"prefeitura_municipal_de_{mun_norm}" in rz:
            score += 90
        if f"camara_municipal_de_{mun_norm}" in rz:
            score += 80
        if "fundo_municipal" in rz:
            score += 30
        if "consorcio_intermunicipal" in rz:
            score += 20
        if "condominio" in rz:
            score -= 50

        ranked.append((score, {"cnpj": cnpj, "razao": razao}))

    ranked.sort(key=lambda x: x[0], reverse=True)
    return [r[1] for r in ranked[:CFG_MAX_ORGAOS_POR_MUNICIPIO]]


@st.cache_data(ttl=1800, show_spinner=False)
def _listar_arquivos_compra(cnpj: str, ano: int, seq: int) -> List[Dict]:
    url = BASE_API_ARQUIVOS.format(cnpj=cnpj, ano=ano, seq=seq)
    r = requests.get(url, headers=HEADERS, timeout=CFG_CONSULTA_TIMEOUT_HTTP)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    js = r.json()
    return js if isinstance(js, list) else _items_from_json(js)


@st.cache_data(ttl=1800, show_spinner=False)
def _primeiro_item_compra(cnpj: str, ano: int, seq: int) -> Dict:
    url = BASE_API_ITENS.format(cnpj=cnpj, ano=ano, seq=seq)
    r = requests.get(
        url,
        params={"pagina": 1, "tamanhoPagina": 1},
        headers=HEADERS,
        timeout=CFG_CONSULTA_TIMEOUT_HTTP,
    )
    if r.status_code == 404:
        return {}
    r.raise_for_status()
    js = r.json()
    if isinstance(js, list) and js:
        return js[0]
    itens = _items_from_json(js)
    return itens[0] if itens else {}


def _status_compativel_aprox(status_value: str, situacao_item_nome: str) -> bool:
    if not status_value:
        return True

    s = _norm(situacao_item_nome)
    encerrados = {"homologado", "anulado_revogado_cancelado", "deserto", "fracassado"}
    andamento = {"em_andamento", "pendente", ""}

    if status_value == "encerrado":
        return s in encerrados
    if status_value in {"recebendo_proposta", "em_julgamento"}:
        return s in andamento
    return True


def _consultar_pncp_por_municipio_scan(
    municipio_id: str,
    municipio_nome: str,
    municipio_uf: str = "",
    municipio_ibge: str = "",
    status_value: str = "recebendo_proposta",
    tam_pagina: int = TAM_PAGINA_FIXO,
    delay_s: float = 0.02,
) -> List[Dict]:
    del municipio_uf, municipio_ibge
    del tam_pagina  # paginação de busca não existe nesses endpoints; mantido por compatibilidade

    resultados: List[Dict] = []
    vistos = set()
    orgaos = _buscar_orgaos_por_municipio_nome(municipio_nome)
    anos = [datetime.now().year - i for i in range(CFG_ANOS_LOOKBACK + 1)]
    t0 = time.monotonic()
    req_count = 0
    max_seq_scan = min(CFG_MAX_SEQ_SCAN, 80)

    for org in orgaos:
        if (time.monotonic() - t0) >= CFG_CONSULTA_MAX_SEGUNDOS_MUNICIPIO:
            break
        if req_count >= CFG_CONSULTA_MAX_REQUISICOES_MUNICIPIO:
            break
        cnpj = str(org.get("cnpj") or "").strip()
        orgao_nome = str(org.get("razao") or "").strip()
        if len(cnpj) != 14:
            continue

        for ano in anos:
            if (time.monotonic() - t0) >= CFG_CONSULTA_MAX_SEGUNDOS_MUNICIPIO:
                break
            if req_count >= CFG_CONSULTA_MAX_REQUISICOES_MUNICIPIO:
                break
            misses = 0
            for seq in range(1, max_seq_scan + 1):
                if (time.monotonic() - t0) >= CFG_CONSULTA_MAX_SEGUNDOS_MUNICIPIO:
                    break
                if req_count >= CFG_CONSULTA_MAX_REQUISICOES_MUNICIPIO:
                    break
                try:
                    req_count += 1
                    arqs = _listar_arquivos_compra(cnpj, ano, seq)
                except Exception:
                    misses += 1
                    if misses >= CFG_MISS_STREAK_STOP and seq > 30:
                        break
                    continue

                if not arqs:
                    misses += 1
                    if misses >= CFG_MISS_STREAK_STOP and seq > 30:
                        break
                    continue

                misses = 0
                doc = next(
                    (
                        a
                        for a in arqs
                        if "edital" in _norm(a.get("tipoDocumentoNome", ""))
                        or "aviso" in _norm(a.get("tipoDocumentoNome", ""))
                    ),
                    None,
                )
                if not doc:
                    continue

                item0 = {}
                try:
                    if req_count >= CFG_CONSULTA_MAX_REQUISICOES_MUNICIPIO:
                        break
                    req_count += 1
                    item0 = _primeiro_item_compra(cnpj, ano, seq)
                except Exception:
                    item0 = {}

                sit_nome = str(item0.get("situacaoCompraItemNome") or "")
                if not _status_compativel_aprox(status_value, sit_nome):
                    continue

                uid = f"{cnpj}-{ano}-{seq}"
                if uid in vistos:
                    continue
                vistos.add(uid)

                resultados.append(
                    {
                        "municipio_codigo": municipio_id,
                        "municipio_nome": municipio_nome,
                        "orgao_cnpj": cnpj,
                        "orgao_nome": orgao_nome,
                        "ano": str(ano),
                        "numero_sequencial": str(seq),
                        "title": doc.get("titulo") or f"Edital {ano}/{seq}",
                        "description": item0.get("descricao") or "",
                        "document_type": doc.get("tipoDocumentoNome") or "",
                        "data_publicacao_pncp": doc.get("dataPublicacaoPncp") or item0.get("dataInclusao") or "",
                        "situacao_compra_item_nome": sit_nome,
                        "item_url": f"/app/editais/{cnpj}/{ano}/{seq}",
                        "id": uid,
                    }
                )
                time.sleep(delay_s)

    return resultados


def consultar_pncp_por_municipio(
    municipio_id: str,
    municipio_nome: str,
    municipio_uf: str = "",
    municipio_ibge: str = "",
    status_value: str = "recebendo_proposta",
    tam_pagina: int = TAM_PAGINA_FIXO,
    delay_s: float = 0.02,
) -> List[Dict]:
    # 1) Fluxo principal: API de Consultas oficial (/api/consulta/v1)
    try:
        return _consultar_pncp_api_consulta(
            municipio_id=municipio_id,
            municipio_nome=municipio_nome,
            municipio_uf=municipio_uf,
            municipio_ibge=municipio_ibge,
            status_value=status_value,
        )
    except Exception:
        if not CFG_ENABLE_FALLBACK_SCAN:
            return []
        # 2) Fallback: endpoints do manual de integração (/pncp-api/v1/orgaos/...)
        return _consultar_pncp_por_municipio_scan(
            municipio_id=municipio_id,
            municipio_nome=municipio_nome,
            municipio_uf=municipio_uf,
            municipio_ibge=municipio_ibge,
            status_value=status_value,
            tam_pagina=tam_pagina,
            delay_s=delay_s,
        )

    return []


def montar_registro(item: Dict, municipio_codigo: str, municipio_nome: str, municipio_uf: str = "") -> Dict:
    pub_raw = item.get("data_publicacao_pncp") or item.get("data") or item.get("dataPublicacao") or ""
    fim_raw = item.get("data_fim_vigencia") or item.get("dataEncerramentoProposta") or item.get("fimEnvioProposta") or ""

    processo = _primeiro_valor(
        item.get("numeroProcesso"),
        item.get("processo"),
        item.get("numero_processo"),
    )

    orgao_cnpj = str(item.get("orgao_cnpj") or "").strip()
    ano = str(item.get("ano") or "").strip()
    seq = str(item.get("numero_sequencial") or "").strip()
    raw_id = str(item.get("id") or item.get("uuid") or "")

    return {
        "municipio_codigo": municipio_codigo,
        "Cidade": item.get("municipio_nome", "") or municipio_nome,
        "UF": item.get("uf", "") or municipio_uf,
        "Título": item.get("title", "") or item.get("titulo", ""),
        "Objeto": item.get("description", "") or item.get("objeto", ""),
        "Link para o edital": _build_pncp_link(
            {"orgao_cnpj": orgao_cnpj, "ano": ano, "numero_sequencial": seq, "item_url": item.get("item_url", "")}
        ),
        "Modalidade": item.get("modalidade_licitacao_nome", ""),
        "Tipo": item.get("tipo_nome", ""),
        "Tipo (documento)": item.get("document_type", ""),
        "Orgão": item.get("orgao_nome", "") or item.get("orgao", ""),
        "Unidade": item.get("unidade_nome", ""),
        "Esfera": item.get("esfera_nome", ""),
        "Publicação": _fmt_dt_iso_to_br(pub_raw),
        "Fim do envio de proposta": _fmt_dt_iso_to_br(fim_raw),
        "numero_processo": str(processo or "").strip(),
        "_pub_raw": pub_raw,
        "_orgao_cnpj": orgao_cnpj,
        "_ano": ano,
        "_seq": seq,
        "_id": raw_id,
    }

# ==========================
# Estado
# ==========================
def _ensure_session_state():
    if "selected_municipios" not in st.session_state:
        st.session_state.selected_municipios = []
    if "saved_searches" not in st.session_state:
        st.session_state.saved_searches = _load_saved_searches()
    if "sidebar_inputs" not in st.session_state:
        st.session_state.sidebar_inputs = {
            "palavra_chave": "",
            "status_label": STATUS_LABELS[0],
            "uf": UF_PLACEHOLDER,
            "save_name": "",
            "selected_saved": None,
        }
    if "uf_prev" not in st.session_state:
        st.session_state.uf_prev = UF_PLACEHOLDER
    if "municipio_nonce" not in st.session_state:
        st.session_state.municipio_nonce = 0
    if "card_page" not in st.session_state:
        st.session_state.card_page = 1
    if "page_size_cards" not in st.session_state:
        st.session_state.page_size_cards = 10
    if "results_df" not in st.session_state:
        st.session_state.results_df = None
    if "results_signature" not in st.session_state:
        st.session_state.results_signature = None
    if "tr_marks" not in st.session_state:
        st.session_state.tr_marks = _load_marks(SAVED_TR_PATH, "tr_marks.json")
    if "na_marks" not in st.session_state:
        st.session_state.na_marks = _load_marks(SAVED_NA_PATH, "na_marks.json")

# ==========================
# Coleta agregada
# ==========================
@st.cache_data(ttl=900, show_spinner=False)
def coletar_por_assinatura(signature: dict) -> pd.DataFrame:
    registros: List[Dict] = []
    municipios_meta = signature.get("municipios_meta", [])
    status_value = signature.get("status", "")
    for m in municipios_meta:
        codigo = str(m.get("codigo_pncp") or "").strip()
        nome = str(m.get("nome") or "").strip()
        uf = str(m.get("uf") or "").strip()
        codigo_ibge = str(m.get("codigo_ibge") or "").strip()
        if not codigo or not nome:
            continue
        try:
            itens = consultar_pncp_por_municipio(
                municipio_id=codigo,
                municipio_nome=nome,
                municipio_uf=uf,
                municipio_ibge=codigo_ibge,
                status_value=status_value,
                tam_pagina=TAM_PAGINA_FIXO,
            )
        except Exception:
            itens = []
        for it in itens:
            registros.append(montar_registro(it, codigo, nome, uf))

    df = pd.DataFrame(registros)

    q = (signature.get("q") or "").strip()
    if q and not df.empty:
        mask = (
            df["Título"].fillna("").str.contains(q, case=False, na=False)
            | df["Objeto"].fillna("").str.contains(q, case=False, na=False)
        )
        df = df[mask].copy()

    try:
        df["_pub_dt"] = pd.to_datetime(df["_pub_raw"], errors="coerce", utc=False)
    except Exception:
        df["_pub_dt"] = pd.NaT
    df.sort_values("_pub_dt", ascending=False, na_position="last", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df

# ==========================
# Sidebar
# ==========================
def _sidebar(pncp_df: pd.DataFrame, ibge_df: Optional[pd.DataFrame]):
    st.sidebar.header("🔎 Filtros")

    palavra = st.sidebar.text_input(
        "Palavra-chave (aplicada no título/objeto):",
        value=st.session_state.sidebar_inputs["palavra_chave"],
        key="palavra_chave_input",
    )
    status_label = st.sidebar.radio(
        "Status",
        STATUS_LABELS,
        index=STATUS_LABELS.index(st.session_state.sidebar_inputs["status_label"]) if st.session_state.sidebar_inputs["status_label"] in STATUS_LABELS else 0,
        key="status_radio",
        help="Agrupamentos mapeados para valores aceitos pela API do PNCP.",
    )

    if ibge_df is not None:
        ufs = sorted(ibge_df["uf"].dropna().unique().tolist())
    else:
        ufs = sorted([u for u in pncp_df.get("uf", pd.Series([], dtype=str)).dropna().unique().tolist() if u])

    ufs = [UF_PLACEHOLDER] + ufs

    uf = st.sidebar.selectbox(
        "Estado (UF) — Obrigatório:",
        ufs,
        index=ufs.index(st.session_state.sidebar_inputs["uf"]) if st.session_state.sidebar_inputs["uf"] in ufs else 0,
        key="uf_select",
    )

    if uf != st.session_state.uf_prev:
        st.session_state.uf_prev = uf
        st.session_state.municipio_nonce += 1

    st.sidebar.markdown("**Municípios (máx. 25)**")
    if uf == UF_PLACEHOLDER:
        st.sidebar.info("Selecione um Estado (UF) para habilitar a seleção de municípios.")
        chosen = None
        add_clicked = False
    else:
        if ibge_df is not None:
            df_show = ibge_df[ibge_df["uf"] == uf].copy()
            df_show["label"] = df_show["municipio"] + " / " + df_show["uf"]
            mun_options = df_show[["municipio", "uf", "label"]].values.tolist()
        else:
            df_temp = pncp_df.copy()
            if "uf" in df_temp.columns:
                df_temp = df_temp[df_temp["uf"].str.upper() == uf.upper()]
            df_temp["label"] = df_temp["nome"] + " / " + df_temp.get("uf", "")
            mun_options = df_temp[["nome", "uf", "label"]].values.tolist()

        labels = ["—"] + [row[2] for row in mun_options]
        chosen = st.sidebar.selectbox(
            "Adicionar município:",
            labels,
            index=0,
            key=f"municipio_select_{st.session_state.municipio_nonce}",
        )
        add_clicked = st.sidebar.button(
            "➕ Adicionar município", use_container_width=True, key=f"add_mun_btn_{st.session_state.municipio_nonce}"
        )

    if add_clicked:
        if chosen == "—":
            st.sidebar.warning("Selecione um município antes de adicionar.")
        else:
            sel_row = next((row for row in mun_options if row[2] == chosen), None)
            if sel_row:
                nome_sel, uf_sel, _ = sel_row
                _add_municipio_by_name(nome_sel, uf_sel, pncp_df)

    if st.session_state.selected_municipios:
        st.sidebar.caption("Selecionados:")
        keep_list = []
        for m in st.session_state.selected_municipios:
            c1, c2 = st.sidebar.columns([0.82, 0.18])
            with c1:
                st.markdown(f"- **{m['nome']}** / {m.get('uf','')} (`{m['codigo_pncp']}`)")
            with c2:
                if st.button("✕", key=f"rm_{m['codigo_pncp']}", help=f"Remover {m['nome']}"):
                    pass
                else:
                    keep_list.append(m)
        if len(keep_list) != len(st.session_state.selected_municipios):
            st.session_state.selected_municipios = keep_list
            st.rerun()

    st.sidebar.subheader("💾 Pesquisa salva")
    save_name = st.sidebar.text_input(
        "Nome da pesquisa", value=st.session_state.sidebar_inputs["save_name"], key="save_name_input"
    )
    col_s1, col_s2 = st.sidebar.columns(2)
    with col_s1:
        salvar = st.button("Salvar", use_container_width=True, key="btn_salvar")
    with col_s2:
        excluir = st.button("Excluir", use_container_width=True, key="btn_excluir")

    if excluir:
        name = save_name.strip()
        if name and name in st.session_state.saved_searches:
            del st.session_state.saved_searches[name]
            _persist_saved_searches(st.session_state.saved_searches)
            st.sidebar.success(f"Pesquisa '{name}' excluída.")
        else:
            st.sidebar.error("Informe o nome exato de uma pesquisa salva para excluir.")

    if salvar:
        name = save_name.strip()
        if not name:
            st.sidebar.error("Informe um nome para salvar.")
        else:
            st.session_state.saved_searches[name] = {
                "palavra_chave": palavra,
                "status_label": status_label,
                "uf": uf,
                "municipios": st.session_state.selected_municipios,
            }
            _persist_saved_searches(st.session_state.saved_searches)
            st.sidebar.success(f"Pesquisa '{name}' salva.")

    st.sidebar.subheader("📚 Pesquisas salvas")
    saved_names = sorted(list(st.session_state.saved_searches.keys()))
    selected_saved = st.sidebar.selectbox("Carregar pesquisa", ["—"] + saved_names, index=0, key="select_saved")
    carregar = st.sidebar.button("Carregar", use_container_width=True, key="btn_carregar")

    if carregar:
        sel = selected_saved
        if sel and sel != "—":
            payload = st.session_state.saved_searches.get(sel, {})
            if payload:
                st.session_state.sidebar_inputs["palavra_chave"] = payload.get("palavra_chave", "")
                st.session_state.sidebar_inputs["status_label"] = (
                    payload.get("status_label", STATUS_LABELS[0])
                    if payload.get("status_label", STATUS_LABELS[0]) in STATUS_LABELS
                    else STATUS_LABELS[0]
                )
                st.session_state.sidebar_inputs["uf"] = payload.get("uf", UF_PLACEHOLDER)
                st.session_state.uf_prev = st.session_state.sidebar_inputs["uf"]
                st.session_state.municipio_nonce += 1
                st.session_state.selected_municipios = payload.get("municipios", [])
                st.session_state.sidebar_inputs["save_name"] = sel
                st.sidebar.success(f"Pesquisa '{sel}' carregada.")
                st.rerun()

    st.session_state.sidebar_inputs["palavra_chave"] = palavra
    st.session_state.sidebar_inputs["status_label"] = status_label
    st.session_state.sidebar_inputs["uf"] = uf
    st.session_state.sidebar_inputs["save_name"] = save_name
    st.session_state.sidebar_inputs["selected_saved"] = selected_saved

    disparar_busca = st.sidebar.button("🔎 Pesquisar", use_container_width=True, type="primary", key="btn_pesquisar")
    if disparar_busca and uf == UF_PLACEHOLDER:
        st.sidebar.error("Selecione uma UF para habilitar a pesquisa.")
        disparar_busca = False

    return disparar_busca

# ==========================
# Helpers
# ==========================
def _add_municipio_by_name(nome_municipio: str, uf: Optional[str], pncp_df: pd.DataFrame) -> None:
    if not nome_municipio:
        return
    sel = st.session_state.selected_municipios
    if len(sel) >= 25:
        st.warning("Limite de 25 municípios por pesquisa atingido.")
        return
    nome_norm = _norm(nome_municipio)
    candidates = pncp_df.copy()
    if "uf" in candidates.columns and uf and uf != "Todos":
        candidates = candidates[candidates["uf"].str.upper() == str(uf).upper()]
    candidates = candidates[candidates["nome_norm"] == nome_norm]
    if candidates.empty:
        candidates = pncp_df[pncp_df["nome_norm"] == nome_norm]
    if candidates.empty:
        st.error(f"Não localizei o município '{nome_municipio}' na planilha PNCP para resolver o código.")
        return
    row = candidates.iloc[0]
    codigo = row["codigo_pncp"]
    nome = row["nome"]
    uf_val = row.get("uf", uf or "")
    codigo_ibge = str(row.get("codigo_ibge", "") or "").strip()
    if codigo in [m["codigo_pncp"] for m in sel]:
        return
    sel.append({"codigo_pncp": codigo, "nome": nome, "uf": uf_val, "codigo_ibge": codigo_ibge})

def _cb_prev(total_pages: int):
    st.session_state.card_page = max(1, int(st.session_state.get("card_page", 1)) - 1)

def _cb_next(total_pages: int):
    st.session_state.card_page = min(total_pages, int(st.session_state.get("card_page", 1)) + 1)

def _cb_page_size_change():
    st.session_state.card_page = 1

# ==========================
# UI principal
# ==========================
def main():
    st.title("📑 Acerte Licitações — O seu Buscador de Editais")
    st.caption("Selecione os filtros desejados como palavra-chave no título/objeto, selecione o Estado (UF) e até 25 municípios. Os editais serão exibidos abaixo, em ordem de publicação.")

    st.markdown('''
        <style>
        section[data-testid="stSidebar"] {
          background: #eef4ff !important;
          border-right: 1px solid #dfe8ff;
          min-width: 360px !important;
          max-width: 360px !important;
        }
        section[data-testid="stSidebar"] * { color: #112a52 !important; }
        section[data-testid="stSidebar"] .stButton > button[kind="primary"] {
          color: #ffffff !important;
          background: #1f4ba8 !important;
          border: 1px solid #173a83 !important;
        }
        section[data-testid="stSidebar"] .stButton > button[kind="primary"]:hover {
          background: #173a83 !important;
          border-color: #122e67 !important;
        }
        header[data-testid="stHeader"] { background: transparent !important; box-shadow: none !important; height: 3rem; }
        div.block-container { padding-top: 2.1rem; background: #f7faff; padding-bottom: 2rem; }
        .stDownloadButton > button {
          color: #ffffff !important;
          background: #1f4ba8 !important;
          border: 1px solid #173a83 !important;
          font-size: 0.7rem !important;
          padding: 0.28rem 0.6rem !important;
        }
        .stDownloadButton > button:hover {
          background: #173a83 !important;
          border-color: #122e67 !important;
        }
        .ac-card {
          background: #f8fbff;
          border: 1.25px solid #cad9f3;
          border-radius: 18px;
          padding: 1.05rem 1.2rem;
          margin-bottom: 1rem;
          box-shadow: 0 8px 20px rgba(20, 45, 110, 0.06);
          transition: box-shadow 0.15s ease, transform 0.15s ease;
        }
        .ac-card:hover {
          box-shadow: 0 10px 24px rgba(20, 45, 110, 0.10);
          transform: translateY(-1px);
        }
        .ac-card h3 { margin-top: 0; margin-bottom: 0.25rem; font-size: 1.08rem; color: #0b1b36; }
        .ac-muted { color: #415477; font-size: 0.92rem; }
        .ac-badge {
          background: #eaf1ff; border: 1px solid #bcd0f7; color: #0b3b8a;
          padding: 0.18rem 0.5rem; border-radius: 999px; font-size: 0.82rem;
        }
        .ac-flag {
          background: #e3f9e5; border: 1px solid #57b26a; color: #1b6f37;
          padding: 0.18rem 0.5rem; border-radius: 999px; font-size: 0.82rem; margin-left: 0.5rem;
        }
        .ac-flag-na {
          background: #fde8e7; border: 1px solid #dc5a5a; color: #9b1c1c;
          padding: 0.18rem 0.5rem; border-radius: 999px; font-size: 0.82rem; margin-left: 0.5rem;
        }
        .ac-link {
          text-decoration:none; padding:0.46rem 0.85rem; border-radius:10px;
          border:1px solid #96b3e9; color:#0b3b8a;
        }
        </style>
    ''', unsafe_allow_html=True)

    _ensure_session_state()

    try:
        pncp_df = load_municipios_pncp()
    except Exception as e:
        st.error(f"Erro ao carregar 'ListaMunicipiosPNCP.csv': {e}")
        st.stop()
    ibge_df = load_ibge_catalog()

    disparar_busca = _sidebar(pncp_df, ibge_df)

    # Enriquecimento retrocompatível para pesquisas salvas antigas (sem codigo_ibge)
    if st.session_state.selected_municipios:
        mapa_ibge = {}
        if "codigo_pncp" in pncp_df.columns and "codigo_ibge" in pncp_df.columns:
            try:
                mapa_ibge = (
                    pncp_df[["codigo_pncp", "codigo_ibge"]]
                    .drop_duplicates(subset=["codigo_pncp"])
                    .set_index("codigo_pncp")["codigo_ibge"]
                    .to_dict()
                )
            except Exception:
                mapa_ibge = {}
        for m in st.session_state.selected_municipios:
            if not str(m.get("codigo_ibge") or "").strip():
                m["codigo_ibge"] = str(mapa_ibge.get(str(m.get("codigo_pncp") or "").strip(), "") or "").strip()

    status_value = STATUS_MAP.get(st.session_state.sidebar_inputs["status_label"], "")
    palavra_chave = (st.session_state.sidebar_inputs["palavra_chave"] or "").strip()
    signature = {
        "municipios": [m["codigo_pncp"] for m in st.session_state.selected_municipios],
        "municipios_meta": [
            {
                "codigo_pncp": str(m.get("codigo_pncp") or "").strip(),
                "nome": str(m.get("nome") or "").strip(),
                "uf": str(m.get("uf") or "").strip(),
                "codigo_ibge": str(m.get("codigo_ibge") or "").strip(),
            }
            for m in st.session_state.selected_municipios
        ],
        "status": status_value,
        "q": palavra_chave.lower(),
    }

    if disparar_busca:
        if not signature["municipios"]:
            st.warning("Selecione pelo menos um município para pesquisar.")
            st.stop()
        with st.spinner("Coletando dados no PNCP..."):
            df = coletar_por_assinatura(signature)
        st.session_state.results_df = df.to_dict("records")
        st.session_state.results_signature = signature
        st.session_state.card_page = 1
    else:
        if st.session_state.results_df is None:
            st.info("Configure os filtros e clique em **Pesquisar**.")
            st.stop()
        df = pd.DataFrame(st.session_state.results_df)
        if st.session_state.results_signature and signature != st.session_state.results_signature:
            st.warning("Filtros alterados após a última coleta. Clique em **Pesquisar** para atualizar os resultados.")

    st.subheader(f"Resultados ({len(df)})")
    if df.empty:
        st.info("Nenhum resultado encontrado com os critérios atuais.")
        return

    if "_pub_dt" not in df.columns:
        try:
            df["_pub_dt"] = pd.to_datetime(df["_pub_raw"], errors="coerce", utc=False)
        except Exception:
            df["_pub_dt"] = pd.NaT
        df.sort_values("_pub_dt", ascending=False, na_position="last", inplace=True)
        df.reset_index(drop=True, inplace=True)

    st.selectbox(
        "Itens por página",
        [10, 20, 50],
        index=[10, 20, 50].index(st.session_state.get("page_size_cards", 10)) if st.session_state.get("page_size_cards", 10) in [10, 20, 50] else 0,
        key="page_size_cards",
        on_change=_cb_page_size_change,
    )
    page_size_cards = int(st.session_state.get("page_size_cards", 10))

    total_items = len(df)
    total_pages = max(1, (total_items + page_size_cards - 1) // page_size_cards)

    col_a, col_b, col_c = st.columns([1, 2, 1])
    with col_a:
        st.button("◀ Anterior", key="prev_top", disabled=(st.session_state.get("card_page", 1) <= 1),
                  on_click=_cb_prev, kwargs={"total_pages": total_pages})
    with col_c:
        st.button("Próxima ▶", key="next_top", disabled=(st.session_state.get("card_page", 1) >= total_pages),
                  on_click=_cb_next, kwargs={"total_pages": total_pages})
    with col_b:
        st.markdown(f"**Página {st.session_state.get('card_page',1)} de {total_pages}**")

    start = (st.session_state.get("card_page", 1) - 1) * page_size_cards
    end = start + page_size_cards
    page_df = df.iloc[start:end].copy()

    for _, row in page_df.iterrows():
        uid = _uid_from_row(row)
        tr_flag = bool(st.session_state.tr_marks.get(uid, False))
        na_flag = bool(st.session_state.na_marks.get(uid, False))

        _, col_cb_tr, col_cb_na = st.columns([6, 1.3, 1.3])
        with col_cb_tr:
            new_tr = st.checkbox("TR Elaborado", value=tr_flag, key=f"tr_{uid}")
        with col_cb_na:
            new_na = st.checkbox("Não Atende", value=na_flag, key=f"na_{uid}")

        updated = False
        if new_tr != tr_flag:
            st.session_state.tr_marks[uid] = bool(new_tr)
            _persist_marks(SAVED_TR_PATH, "tr_marks.json", st.session_state.tr_marks)
            updated = True
        if new_na != na_flag:
            st.session_state.na_marks[uid] = bool(new_na)
            _persist_marks(SAVED_NA_PATH, "na_marks.json", st.session_state.na_marks)
            updated = True
        if updated:
            st.rerun()

        link = row.get("Link para o edital", "")
        titulo = row.get("Título") or "(Sem título)"
        cidade = row.get("Cidade", "")
        uf = row.get("UF", "")
        pub = row.get("Publicação", "")
        fim = row.get("Fim do envio de proposta", "")
        objeto = row.get("Objeto", "")
        modalidade = row.get("Modalidade", "")
        tipo = row.get("Tipo", "")
        orgao = row.get("Orgão", "")
        proc = (row.get("numero_processo") or "").strip()

        tr_badge = '<span class="ac-flag">TR Elaborado</span>' if new_tr else ''
        na_badge = '<span class="ac-flag-na">Não Atende</span>' if new_na else ''
        processo_html = f'<div class="ac-muted">Processo: {proc}</div>' if proc else '<div></div>'

        html = f'''
        <div class="ac-card">
            <h3>{titulo} {tr_badge} {na_badge}</h3>
            <div class="ac-muted">
                <span class="ac-badge">{cidade} / {uf}</span>
                &nbsp;•&nbsp;
                <strong>Publicação:</strong> {pub}
                &nbsp;|&nbsp;
                <strong>Fim do envio:</strong> {fim}
            </div>
            <div style="margin-top:0.55rem;"><strong>Objeto:</strong> {objeto}</div>
            <div style="display:flex; gap:1rem; margin-top:0.55rem; flex-wrap:wrap;">
                <div><strong>Modalidade:</strong> {modalidade}</div>
                <div><strong>Tipo:</strong> {tipo}</div>
                <div><strong>Órgão:</strong> {orgao}</div>
            </div>
            <div style="display:flex; justify-content:space-between; align-items:center; margin-top:0.7rem;">
                {processo_html}
                {f'<a class="ac-link" href="{link}" target="_blank">Abrir edital</a>' if isinstance(link, str) and link else ''}
            </div>
        </div>
        '''
        st.markdown(html, unsafe_allow_html=True)

    col_a2, col_b2, col_c2 = st.columns([1, 2, 1])
    with col_a2:
        st.button("◀ Anterior", key="prev_bottom", disabled=(st.session_state.get("card_page", 1) <= 1),
                  on_click=_cb_prev, kwargs={"total_pages": total_pages})
    with col_c2:
        st.button("Próxima ▶", key="next_bottom", disabled=(st.session_state.get("card_page", 1) >= total_pages),
                  on_click=_cb_next, kwargs={"total_pages": total_pages})
    with col_b2:
        st.markdown(f"**Página {st.session_state.get('card_page',1)} de {total_pages}**")

    st.divider()

    drop_cols = [c for c in ["_pub_raw", "_fim_raw", "_pub_dt", "_orgao_cnpj", "_ano", "_seq", "_id"] if c in df.columns]
    export_df = df.drop(columns=[c for c in drop_cols if c in df.columns]).copy()
    xlsx_buf = io.BytesIO()
    with pd.ExcelWriter(xlsx_buf, engine="openpyxl") as wr:
        export_df.to_excel(wr, index=False, sheet_name="PNCP")
    xlsx_bytes = xlsx_buf.getvalue()

    st.markdown("### ⬇️ Baixar planilha")
    st.download_button(
        "Baixar XLSX",
        data=xlsx_bytes,
        file_name=f"pncp_resultados_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
        type="primary",
    )

if __name__ == "__main__":
    main()


