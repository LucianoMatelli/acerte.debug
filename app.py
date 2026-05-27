# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import io
import re
import json
import base64
import hashlib
import unicodedata
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
# Constantes
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

SAVED_SEARCHES_PATHS = [
    os.path.join(DATA_DIR, "saved_searches.json"),
    os.path.join(BASE_DIR, "saved_searches.json"),
]
TR_MARKS_PATHS = [
    os.path.join(DATA_DIR, "tr_marks.json"),
    os.path.join(BASE_DIR, "tr_marks.json"),
]
NA_MARKS_PATHS = [
    os.path.join(DATA_DIR, "na_marks.json"),
    os.path.join(BASE_DIR, "na_marks.json"),
]

ORIGIN = "https://pncp.gov.br"
API_DADOS_ABERTOS = "https://dadosabertos.compras.gov.br/modulo-contratacoes/1_consultarContratacoes_PNCP_14133"
API_IBGE_LOCALIDADES = "https://servicodados.ibge.gov.br/api/v1/localidades/estados/{uf}/municipios"

HEADERS = {
    "User-Agent": "AcerteLicitacoesBackup/1.0 (+streamlit)",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pt-BR,pt;q=0.9",
}

# Manual API Compras (10.1 - Indicadores de Modalidade da Compra)
# 01,02,03,04,05,06,07,12,20,22,33,44,57
MODALIDADES_PADRAO = [1, 2, 3, 4, 5, 6, 7, 12, 20, 22, 33, 44, 57]

# Layout/Comportamento
TAM_PAGINA_CARDS_PADRAO = 10
TAM_PAGINA_API_PADRAO = 200
MAX_PAGINAS_MODALIDADE_PADRAO = 4
LOOKBACK_DIAS_PADRAO = 365  # limite do endpoint
MAX_MUNICIPIOS = 25

# GitHub fallback (repo público de teste)
DEFAULT_GITHUB_REPO = "LucianoMatelli/acerte.debug"
DEFAULT_GITHUB_BRANCH = "main"
DEFAULT_GITHUB_BASEDIR = "data"

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
def _norm(s: str) -> str:
    s = str(s or "").strip().lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")


def _safe_text(v) -> str:
    return str(v or "").strip()


def _secret_int(name: str, default: int) -> int:
    try:
        v = int(st.secrets.get(name, default))
        return v if v > 0 else default
    except Exception:
        return default


def _secret_int_list(name: str, default: List[int]) -> List[int]:
    raw = str(st.secrets.get(name, "") or "").strip()
    if not raw:
        return list(default)
    out: List[int] = []
    for p in raw.split(","):
        try:
            v = int(p.strip())
            if v > 0:
                out.append(v)
        except Exception:
            continue
    uniq = sorted(set(out))
    return uniq if uniq else list(default)


CFG_TAM_PAGINA_API = min(500, _secret_int("BACKUP_TAM_PAGINA_API", TAM_PAGINA_API_PADRAO))
CFG_MAX_PAGINAS_MODALIDADE = _secret_int("BACKUP_MAX_PAGINAS_MODALIDADE", MAX_PAGINAS_MODALIDADE_PADRAO)
CFG_LOOKBACK_DIAS = min(365, _secret_int("BACKUP_LOOKBACK_DIAS", LOOKBACK_DIAS_PADRAO))
CFG_TIMEOUT = _secret_int("BACKUP_TIMEOUT_HTTP", 20)
CFG_MODALIDADES = _secret_int_list("BACKUP_MODALIDADES", MODALIDADES_PADRAO)
CFG_APENAS_EDITAL = str(st.secrets.get("BACKUP_APENAS_EDITAL", "true")).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


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


def _public_edital_link(cnpj: str, ano: str, seq: str) -> str:
    if len(cnpj) == 14 and str(ano).isdigit() and str(seq):
        return f"{ORIGIN}/app/editais/{cnpj}/{ano}/{seq}"
    return ""


def _parse_numero_controle(numero_controle: str) -> Tuple[str, str, str]:
    # Manual API Consultas PNCP v1.0:
    # contratação => 99999999999999-1-999999/9999
    n = _safe_text(numero_controle)
    m = re.search(r"^(\d{14})-1-(\d+)/(\d{4})$", n)
    if not m:
        return "", "", ""
    cnpj = m.group(1)
    seq = m.group(2)
    ano = m.group(3)
    return cnpj, ano, seq


def _uid_from_row(row: Dict) -> str:
    cnpj = _safe_text(row.get("_orgao_cnpj"))
    ano = _safe_text(row.get("_ano"))
    seq = _safe_text(row.get("_seq"))
    if len(cnpj) == 14 and ano.isdigit() and seq:
        return f"{cnpj}-{ano}-{seq}"
    base = "|".join(
        [
            _safe_text(row.get("Título")),
            _safe_text(row.get("Cidade")),
            _safe_text(row.get("UF")),
            _safe_text(row.get("_pub_raw")),
            _safe_text(row.get("Orgão")),
        ]
    )
    return hashlib.md5(base.encode("utf-8")).hexdigest()


def _load_json_from_candidates(paths: List[str]) -> Optional[dict]:
    for p in paths:
        try:
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    js = json.load(f)
                if isinstance(js, dict):
                    return js
        except Exception:
            continue
    return None


def _save_json_to_candidates(paths: List[str], payload: dict) -> None:
    target = ""
    for p in paths:
        try:
            if os.path.exists(p):
                target = p
                break
        except Exception:
            continue
    if not target:
        target = paths[0]
    parent = os.path.dirname(target)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


# ==========================
# Persistência GitHub + local
# ==========================
def _gh_repo() -> str:
    return (
        _safe_text(st.secrets.get("GITHUB_REPO_TEST"))
        or _safe_text(st.secrets.get("GITHUB_REPO"))
        or _safe_text(os.getenv("GITHUB_REPOSITORY"))
        or DEFAULT_GITHUB_REPO
    )


def _gh_branch() -> str:
    return (
        _safe_text(st.secrets.get("GITHUB_BRANCH_TEST"))
        or _safe_text(st.secrets.get("GITHUB_BRANCH"))
        or DEFAULT_GITHUB_BRANCH
    )


def _gh_basedir() -> str:
    base_test = st.secrets.get("GITHUB_BASEDIR_TEST")
    if base_test is not None:
        return _safe_text(base_test)
    return _safe_text(st.secrets.get("GITHUB_BASEDIR")) or DEFAULT_GITHUB_BASEDIR


def _gh_headers() -> Dict[str, str]:
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "AcerteLicitacoesBackup/1.0",
    }
    token = _safe_text(st.secrets.get("GITHUB_TOKEN"))
    if token:
        h["Authorization"] = f"token {token}"
    return h


def _gh_path(filename: str) -> Tuple[str, str, str]:
    repo = _gh_repo()
    branch = _gh_branch()
    basedir = _gh_basedir()
    path = f"{basedir.rstrip('/')}/{filename}" if basedir else filename
    return repo, branch, path


def _gh_get_json(filename: str) -> Tuple[Optional[dict], Optional[str]]:
    repo, branch, path = _gh_path(filename)

    # 1) GitHub Contents API
    try:
        url = f"https://api.github.com/repos/{repo}/contents/{path}"
        r = requests.get(url, params={"ref": branch}, headers=_gh_headers(), timeout=20)
        if 200 <= r.status_code < 300:
            js = r.json()
            content_b64 = _safe_text(js.get("content"))
            sha = _safe_text(js.get("sha")) or None
            raw = base64.b64decode(content_b64).decode("utf-8")
            parsed = json.loads(raw)
            return (parsed if isinstance(parsed, dict) else None), sha
    except Exception:
        pass

    # 2) RAW público
    try:
        raw_url = f"https://raw.githubusercontent.com/{repo}/{branch}/{path}"
        rr = requests.get(raw_url, headers={"User-Agent": "AcerteLicitacoesBackup/1.0"}, timeout=20)
        if rr.status_code == 200:
            parsed = json.loads(rr.text)
            if isinstance(parsed, dict):
                return parsed, None
    except Exception:
        pass

    return None, None


def _gh_put_json(filename: str, payload: dict, sha: Optional[str]) -> None:
    token = _safe_text(st.secrets.get("GITHUB_TOKEN"))
    if not token:
        raise RuntimeError("GITHUB_TOKEN ausente para escrita no GitHub")

    repo, branch, path = _gh_path(filename)
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    content_b64 = base64.b64encode(
        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    ).decode("utf-8")

    data = {
        "message": f"chore: atualizar {path} via app backup",
        "content": content_b64,
        "branch": branch,
        "committer": {
            "name": _safe_text(st.secrets.get("GITHUB_COMMITTER_NAME")) or "PNCP Bot",
            "email": _safe_text(st.secrets.get("GITHUB_COMMITTER_EMAIL")) or "bot@acertelicitacoes.local",
        },
    }
    if sha:
        data["sha"] = sha

    r = requests.put(url, headers=_gh_headers(), json=data, timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"GitHub PUT {r.status_code}: {r.text[:180]}")


def _load_saved_searches() -> Dict[str, Dict]:
    js, _ = _gh_get_json("saved_searches.json")
    if isinstance(js, dict):
        return js
    local = _load_json_from_candidates(SAVED_SEARCHES_PATHS)
    return local if isinstance(local, dict) else {}


def _persist_saved_searches(payload: Dict[str, Dict]) -> None:
    try:
        _, sha = _gh_get_json("saved_searches.json")
        _gh_put_json("saved_searches.json", payload, sha)
        return
    except Exception:
        pass
    _save_json_to_candidates(SAVED_SEARCHES_PATHS, payload)


def _load_marks(remote_name: str, local_paths: List[str]) -> Dict[str, bool]:
    js, _ = _gh_get_json(remote_name)
    if isinstance(js, dict):
        return {str(k): bool(v) for k, v in js.items()}
    local = _load_json_from_candidates(local_paths)
    if isinstance(local, dict):
        return {str(k): bool(v) for k, v in local.items()}
    return {}


def _persist_marks(remote_name: str, local_paths: List[str], payload: Dict[str, bool]) -> None:
    try:
        _, sha = _gh_get_json(remote_name)
        _gh_put_json(remote_name, payload, sha)
        return
    except Exception:
        pass
    _save_json_to_candidates(local_paths, payload)


# ==========================
# Catálogos locais
# ==========================
@st.cache_data(show_spinner=False)
def load_municipios_pncp() -> pd.DataFrame:
    encodings = ["utf-8", "utf-8-sig", "latin1", "cp1252"]
    seps = [",", ";", "\t", "|"]
    last_err = None

    def _guess_cols(df: pd.DataFrame) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        cols_norm = {_norm(c): c for c in df.columns}
        col_nome = cols_norm.get("municipio") or cols_norm.get("nome") or ("Municipio" if "Municipio" in df.columns else None)
        col_codigo = cols_norm.get("id") or cols_norm.get("codigo") or ("id" if "id" in df.columns else None)
        col_uf = cols_norm.get("uf") or cols_norm.get("estado")
        col_ibge = (
            cols_norm.get("codigo_ibge")
            or cols_norm.get("cod_ibge")
            or cols_norm.get("ibge")
            or cols_norm.get("municipio_ibge")
            or cols_norm.get("codigo_municipio_ibge")
        )
        return col_nome, col_codigo, col_uf, col_ibge

    for path in CSV_PNCP_PATHS:
        if not os.path.exists(path):
            continue
        for enc in encodings:
            for sep in seps:
                try:
                    df = pd.read_csv(path, dtype=str, sep=sep, encoding=enc, engine="python", on_bad_lines="skip")
                    if df is None or df.empty or df.shape[1] == 0:
                        continue
                    col_nome, col_codigo, col_uf, col_ibge = _guess_cols(df)
                    if not col_nome or not col_codigo:
                        continue
                    out = pd.DataFrame({
                        "nome": df[col_nome].astype(str).str.strip(),
                        "codigo_pncp": df[col_codigo].astype(str).str.strip(),
                    })
                    out["uf"] = df[col_uf].astype(str).str.strip().str.upper() if col_uf else ""
                    out["codigo_ibge"] = df[col_ibge].astype(str).str.strip() if col_ibge else ""
                    out["nome_norm"] = out["nome"].map(_norm)
                    out = out[out["codigo_pncp"] != ""].drop_duplicates(subset=["codigo_pncp"]).reset_index(drop=True)
                    return out
                except Exception as e:
                    last_err = e
                    continue

    if last_err:
        raise last_err
    raise FileNotFoundError("ListaMunicipiosPNCP.csv não encontrada em ./data nem na raiz.")


@st.cache_data(show_spinner=False)
def load_ibge_catalog_local() -> Optional[pd.DataFrame]:
    encodings = ["utf-8", "utf-8-sig", "latin1", "cp1252"]
    seps = [",", ";", "\t", "|"]
    for path in CSV_IBGE_PATHS:
        if not os.path.exists(path):
            continue
        for enc in encodings:
            for sep in seps:
                try:
                    df = pd.read_csv(path, dtype=str, sep=sep, encoding=enc, engine="python", on_bad_lines="skip")
                    if df is None or df.empty or df.shape[1] < 2:
                        continue
                    cols = {c.lower().strip(): c for c in df.columns}
                    col_uf = next((cols[k] for k in cols if k in ["uf", "sigla_uf", "estado"]), None)
                    col_mun = next((cols[k] for k in cols if k in ["municipio", "município", "nome"]), None)
                    col_ibge = next(
                        (cols[k] for k in cols if k in ["codigo_ibge", "cod_ibge", "ibge", "codigo", "id_municipio", "municipio_id"]),
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


@st.cache_data(ttl=604800, show_spinner=False)
def ibge_municipios_uf_online(uf: str) -> Dict[str, str]:
    uf_up = _safe_text(uf).upper()
    if len(uf_up) != 2:
        return {}
    try:
        url = API_IBGE_LOCALIDADES.format(uf=uf_up)
        r = requests.get(url, headers={"User-Agent": "AcerteLicitacoesBackup/1.0"}, timeout=20)
        if r.status_code >= 400:
            return {}
        js = r.json()
    except Exception:
        return {}
    out: Dict[str, str] = {}
    if not isinstance(js, list):
        return out
    for it in js:
        nome = _safe_text(it.get("nome"))
        cod = _safe_text(it.get("id"))
        if nome and cod.isdigit():
            out[_norm(nome)] = cod
    return out


def resolver_codigo_ibge(
    nome_municipio: str,
    uf: str,
    pncp_df: pd.DataFrame,
    ibge_local_df: Optional[pd.DataFrame],
    codigo_ibge_existente: str = "",
) -> str:
    if _safe_text(codigo_ibge_existente).isdigit():
        return _safe_text(codigo_ibge_existente)

    nome_norm = _norm(nome_municipio)
    uf_up = _safe_text(uf).upper()
    if not nome_norm or len(uf_up) != 2:
        return ""

    # 1) tenta coluna codigo_ibge da lista PNCP
    try:
        if {"nome_norm", "uf", "codigo_ibge"}.issubset(set(pncp_df.columns)):
            hit = pncp_df[(pncp_df["nome_norm"] == nome_norm) & (pncp_df["uf"].str.upper() == uf_up)]
            if not hit.empty:
                cod = _safe_text(hit.iloc[0].get("codigo_ibge"))
                if cod.isdigit():
                    return cod
    except Exception:
        pass

    # 2) tenta catálogo local IBGE
    try:
        if ibge_local_df is not None and {"uf", "municipio_norm", "codigo_ibge"}.issubset(set(ibge_local_df.columns)):
            hit2 = ibge_local_df[(ibge_local_df["uf"] == uf_up) & (ibge_local_df["municipio_norm"] == nome_norm)]
            if not hit2.empty:
                cod2 = _safe_text(hit2.iloc[0].get("codigo_ibge"))
                if cod2.isdigit():
                    return cod2
    except Exception:
        pass

    # 3) fallback online IBGE
    mapa = ibge_municipios_uf_online(uf_up)
    cod3 = _safe_text(mapa.get(nome_norm))
    return cod3 if cod3.isdigit() else ""


# ==========================
# Busca (API Dados Abertos)
# ==========================
def _status_match(item: Dict, status_value: str) -> bool:
    if not status_value:
        return True

    situacao_id = item.get("situacaoCompraIdPncp")
    try:
        sit_id = int(situacao_id) if situacao_id is not None else None
    except Exception:
        sit_id = None

    sit_nome = _norm(item.get("situacaoCompraNomePncp", ""))
    fim_raw = _safe_text(item.get("dataEncerramentoPropostaPncp"))
    fim_dt = pd.to_datetime(fim_raw, errors="coerce", utc=True)
    if pd.notna(fim_dt):
        fim_dt = fim_dt.tz_localize(None)
    now = pd.Timestamp.now(tz=None)

    proposta_aberta = bool(pd.isna(fim_dt) or fim_dt >= now)
    proposta_encerrada = bool(pd.notna(fim_dt) and fim_dt < now)
    is_cancelada = sit_id in {2, 3, 4} or ("anulad" in sit_nome) or ("revogad" in sit_nome) or ("suspens" in sit_nome)
    existe_resultado = bool(item.get("existeResultado", False))

    if status_value == "recebendo_proposta":
        return (not is_cancelada) and proposta_aberta

    if status_value == "em_julgamento":
        return (not is_cancelada) and proposta_encerrada

    if status_value == "encerrado":
        return is_cancelada or (proposta_encerrada and existe_resultado)

    return True


def _is_edital(item: Dict) -> bool:
    cod = item.get("tipoInstrumentoConvocatorioCodigoPncp")
    nome = _norm(item.get("tipoInstrumentoConvocatorioNome", ""))
    try:
        if cod is not None and int(cod) == 1:
            return True
    except Exception:
        pass
    return nome == "edital"


def _request_contratacoes(params: Dict) -> Tuple[List[Dict], int]:
    r = requests.get(API_DADOS_ABERTOS, params=params, headers=HEADERS, timeout=CFG_TIMEOUT)
    if r.status_code >= 400:
        msg = (r.text or "").strip()
        raise RuntimeError(f"HTTP {r.status_code}: {msg[:140]}")

    ctype = (r.headers.get("content-type") or "").lower()
    body = (r.text or "").strip()
    if "json" not in ctype and not body.startswith("{"):
        raise RuntimeError("Resposta não-JSON do endpoint")
    js = r.json()
    if not isinstance(js, dict):
        return [], 0

    rows = js.get("resultado", [])
    if not isinstance(rows, list):
        rows = []
    total_paginas = int(js.get("totalPaginas") or 0)
    return rows, total_paginas


def _normalizar_item_contratacao(item: Dict, codigo_pncp: str, nome_municipio: str, uf: str) -> Dict:
    numero_controle = _safe_text(item.get("numeroControlePNCP"))
    cnpj, ano_ctrl, seq_ctrl = _parse_numero_controle(numero_controle)

    orgao_cnpj = _safe_text(item.get("orgaoEntidadeCnpj")) or cnpj
    ano = _safe_text(item.get("anoCompraPncp")) or ano_ctrl
    seq = _safe_text(item.get("sequencialCompraPncp")) or seq_ctrl

    tipo_inst = _safe_text(item.get("tipoInstrumentoConvocatorioNome")) or "Edital"
    numero_compra = _safe_text(item.get("numeroCompra"))
    processo = _safe_text(item.get("processo"))
    modalidade = _safe_text(item.get("modalidadeNome"))
    orgao_nome = _safe_text(item.get("orgaoEntidadeRazaoSocial"))
    objeto = _safe_text(item.get("objetoCompra")) or _safe_text(item.get("informacaoComplementar"))
    pub_raw = _safe_text(item.get("dataPublicacaoPncp"))
    fim_raw = _safe_text(item.get("dataEncerramentoPropostaPncp"))
    cidade = _safe_text(item.get("unidadeOrgaoMunicipioNome")) or nome_municipio
    uf_val = _safe_text(item.get("unidadeOrgaoUfSigla")).upper() or uf.upper()

    if numero_compra and ano:
        titulo = f"{tipo_inst} n° {numero_compra}/{ano}"
    elif numero_compra:
        titulo = f"{tipo_inst} n° {numero_compra}"
    else:
        titulo = numero_controle or "(Sem título)"
    if processo:
        titulo = f"{titulo} | Processo {processo}"

    return {
        "municipio_codigo": codigo_pncp,
        "Cidade": cidade,
        "UF": uf_val,
        "Título": titulo,
        "Objeto": objeto,
        "Link para o edital": _public_edital_link(orgao_cnpj, ano, seq),
        "Modalidade": modalidade,
        "Tipo": tipo_inst,
        "Tipo (documento)": tipo_inst,
        "Orgão": orgao_nome,
        "Unidade": _safe_text(item.get("unidadeOrgaoNomeUnidade")),
        "Esfera": _safe_text(item.get("orgaoEntidadeEsferaId")),
        "Publicação": _fmt_dt_iso_to_br(pub_raw),
        "Fim do envio de proposta": _fmt_dt_iso_to_br(fim_raw),
        "numero_processo": processo,
        "_pub_raw": pub_raw,
        "_orgao_cnpj": orgao_cnpj,
        "_ano": ano,
        "_seq": seq,
        "_id": _safe_text(item.get("idCompra")) or numero_controle,
    }


@st.cache_data(ttl=900, show_spinner=False)
def buscar_contratacoes_municipio(
    codigo_pncp: str,
    nome_municipio: str,
    uf: str,
    codigo_ibge: str,
    status_value: str,
    palavra_chave: str,
) -> pd.DataFrame:
    if not codigo_pncp or not nome_municipio:
        return pd.DataFrame()

    data_final = datetime.now().strftime("%Y-%m-%d")
    data_inicial = (datetime.now() - timedelta(days=max(1, CFG_LOOKBACK_DIAS))).strftime("%Y-%m-%d")

    vistos = set()
    registros: List[Dict] = []

    for modalidade in CFG_MODALIDADES:
        for pagina in range(1, max(1, CFG_MAX_PAGINAS_MODALIDADE) + 1):
            params: Dict[str, object] = {
                "pagina": pagina,
                "tamanhoPagina": CFG_TAM_PAGINA_API,
                "dataPublicacaoPncpInicial": data_inicial,
                "dataPublicacaoPncpFinal": data_final,
                "codigoModalidade": int(modalidade),
                "unidadeOrgaoUfSigla": uf.upper(),
                "contratacaoExcluida": "false",
            }
            if _safe_text(codigo_ibge).isdigit():
                params["unidadeOrgaoCodigoIbge"] = int(codigo_ibge)

            try:
                rows, total_paginas = _request_contratacoes(params)
            except Exception:
                break

            if not rows:
                break

            for item in rows:
                if CFG_APENAS_EDITAL and not _is_edital(item):
                    continue

                # reforço de município (quando IBGE não vier no payload por inconsistência de origem)
                cidade_api = _safe_text(item.get("unidadeOrgaoMunicipioNome"))
                uf_api = _safe_text(item.get("unidadeOrgaoUfSigla")).upper()
                if uf_api and uf_api != uf.upper():
                    continue
                if cidade_api and _norm(cidade_api) != _norm(nome_municipio):
                    continue

                if not _status_match(item, status_value):
                    continue

                if palavra_chave:
                    titulo_q = _safe_text(item.get("numeroCompra")) + " " + _safe_text(item.get("processo"))
                    obj_q = _safe_text(item.get("objetoCompra")) + " " + _safe_text(item.get("informacaoComplementar"))
                    txt = f"{titulo_q} {obj_q}".lower()
                    if palavra_chave.lower() not in txt:
                        continue

                key = _safe_text(item.get("idCompra")) or _safe_text(item.get("numeroControlePNCP"))
                if not key:
                    key = hashlib.md5(json.dumps(item, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
                if key in vistos:
                    continue
                vistos.add(key)

                registros.append(_normalizar_item_contratacao(item, codigo_pncp, nome_municipio, uf))

            if total_paginas and pagina >= total_paginas:
                break

    if not registros:
        return pd.DataFrame()

    df = pd.DataFrame(registros)
    try:
        df["_pub_dt"] = pd.to_datetime(df["_pub_raw"], errors="coerce", utc=False)
    except Exception:
        df["_pub_dt"] = pd.NaT
    df.sort_values("_pub_dt", ascending=False, na_position="last", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


@st.cache_data(ttl=900, show_spinner=False)
def coletar_por_assinatura(signature: dict) -> pd.DataFrame:
    registros: List[pd.DataFrame] = []
    for m in signature.get("municipios_meta", []):
        df_m = buscar_contratacoes_municipio(
            codigo_pncp=_safe_text(m.get("codigo_pncp")),
            nome_municipio=_safe_text(m.get("nome")),
            uf=_safe_text(m.get("uf")),
            codigo_ibge=_safe_text(m.get("codigo_ibge")),
            status_value=_safe_text(signature.get("status")),
            palavra_chave=_safe_text(signature.get("q")),
        )
        if not df_m.empty:
            registros.append(df_m)

    if not registros:
        return pd.DataFrame()

    df = pd.concat(registros, ignore_index=True)
    if "_pub_dt" not in df.columns:
        try:
            df["_pub_dt"] = pd.to_datetime(df["_pub_raw"], errors="coerce", utc=False)
        except Exception:
            df["_pub_dt"] = pd.NaT
    df.sort_values("_pub_dt", ascending=False, na_position="last", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


# ==========================
# Estado
# ==========================
def _ensure_session_state() -> None:
    if "selected_municipios" not in st.session_state:
        st.session_state.selected_municipios = []  # [{codigo_pncp,nome,uf,codigo_ibge}]
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
    if "results_df" not in st.session_state:
        st.session_state.results_df = None
    if "results_signature" not in st.session_state:
        st.session_state.results_signature = None
    if "card_page" not in st.session_state:
        st.session_state.card_page = 1
    if "page_size_cards" not in st.session_state:
        st.session_state.page_size_cards = TAM_PAGINA_CARDS_PADRAO
    if "tr_marks" not in st.session_state:
        st.session_state.tr_marks = _load_marks("tr_marks.json", TR_MARKS_PATHS)
    if "na_marks" not in st.session_state:
        st.session_state.na_marks = _load_marks("na_marks.json", NA_MARKS_PATHS)


def _add_municipio_by_name(nome: str, uf: str, pncp_df: pd.DataFrame, ibge_df: Optional[pd.DataFrame]) -> None:
    if not nome:
        return
    if len(st.session_state.selected_municipios) >= MAX_MUNICIPIOS:
        st.warning(f"Limite de {MAX_MUNICIPIOS} municípios por pesquisa atingido.")
        return

    nome_norm = _norm(nome)
    candidatos = pncp_df.copy()
    if "uf" in candidatos.columns and uf:
        candidatos = candidatos[candidatos["uf"].str.upper() == uf.upper()]
    candidatos = candidatos[candidatos["nome_norm"] == nome_norm]
    if candidatos.empty:
        candidatos = pncp_df[pncp_df["nome_norm"] == nome_norm]
    if candidatos.empty:
        st.error(f"Não localizei o município '{nome}' na planilha PNCP.")
        return

    row = candidatos.iloc[0]
    codigo = _safe_text(row.get("codigo_pncp"))
    if not codigo:
        st.error(f"Município '{nome}' sem código PNCP na planilha.")
        return
    if codigo in [m["codigo_pncp"] for m in st.session_state.selected_municipios]:
        return

    codigo_ibge = resolver_codigo_ibge(
        nome_municipio=_safe_text(row.get("nome")) or nome,
        uf=_safe_text(row.get("uf")) or uf,
        pncp_df=pncp_df,
        ibge_local_df=ibge_df,
        codigo_ibge_existente=_safe_text(row.get("codigo_ibge")),
    )
    st.session_state.selected_municipios.append(
        {
            "codigo_pncp": codigo,
            "nome": _safe_text(row.get("nome")) or nome,
            "uf": _safe_text(row.get("uf")) or uf,
            "codigo_ibge": codigo_ibge,
        }
    )


def _normalize_saved_municipios(raw: List[Dict], pncp_df: pd.DataFrame, ibge_df: Optional[pd.DataFrame]) -> List[Dict]:
    out: List[Dict] = []
    for m in raw or []:
        codigo = _safe_text(m.get("codigo_pncp"))
        nome = _safe_text(m.get("nome"))
        uf = _safe_text(m.get("uf")).upper()
        codigo_ibge = _safe_text(m.get("codigo_ibge"))
        if not codigo or not nome:
            continue
        if not codigo_ibge:
            codigo_ibge = resolver_codigo_ibge(nome, uf, pncp_df, ibge_df, "")
        out.append(
            {
                "codigo_pncp": codigo,
                "nome": nome,
                "uf": uf,
                "codigo_ibge": codigo_ibge,
            }
        )
    # remove duplicados por código
    seen = set()
    uniq = []
    for m in out:
        c = m["codigo_pncp"]
        if c in seen:
            continue
        seen.add(c)
        uniq.append(m)
    return uniq


def _sidebar(pncp_df: pd.DataFrame, ibge_df: Optional[pd.DataFrame]) -> bool:
    st.sidebar.header("🔎 Filtros")

    palavra = st.sidebar.text_input(
        "Palavra-chave (aplicada no título/objeto):",
        value=st.session_state.sidebar_inputs["palavra_chave"],
        key="palavra_chave_input",
    )

    status_label = st.sidebar.radio(
        "Status",
        STATUS_LABELS,
        index=STATUS_LABELS.index(st.session_state.sidebar_inputs["status_label"])
        if st.session_state.sidebar_inputs["status_label"] in STATUS_LABELS
        else 0,
        key="status_radio",
        help="Filtro de status aplicado sobre situação da contratação + data de encerramento de proposta.",
    )

    if ibge_df is not None:
        ufs = sorted(ibge_df["uf"].dropna().astype(str).str.upper().unique().tolist())
    else:
        ufs = sorted(pncp_df["uf"].dropna().astype(str).str.upper().unique().tolist()) if "uf" in pncp_df.columns else []
    ufs = [UF_PLACEHOLDER] + [u for u in ufs if u]

    uf = st.sidebar.selectbox(
        "Estado (UF) — Obrigatório:",
        ufs,
        index=ufs.index(st.session_state.sidebar_inputs["uf"]) if st.session_state.sidebar_inputs["uf"] in ufs else 0,
        key="uf_select",
    )

    if uf != st.session_state.uf_prev:
        st.session_state.uf_prev = uf
        st.session_state.municipio_nonce += 1

    st.sidebar.markdown(f"**Municípios (máx. {MAX_MUNICIPIOS})**")
    if uf == UF_PLACEHOLDER:
        st.sidebar.info("Selecione uma UF para habilitar os municípios.")
        add_clicked = False
        chosen = None
        mun_options = []
    else:
        if ibge_df is not None:
            df_show = ibge_df[ibge_df["uf"] == uf].copy()
            df_show["label"] = df_show["municipio"] + " / " + df_show["uf"]
            mun_options = df_show[["municipio", "uf", "label"]].values.tolist()
        else:
            df_temp = pncp_df[pncp_df["uf"].str.upper() == uf.upper()].copy() if "uf" in pncp_df.columns else pncp_df.copy()
            df_temp["label"] = df_temp["nome"] + " / " + uf
            mun_options = df_temp[["nome", "uf", "label"]].values.tolist()

        labels = ["—"] + [r[2] for r in mun_options]
        chosen = st.sidebar.selectbox(
            "Adicionar município:",
            labels,
            index=0,
            key=f"municipio_select_{st.session_state.municipio_nonce}",
        )
        add_clicked = st.sidebar.button(
            "➕ Adicionar município",
            key=f"add_mun_btn_{st.session_state.municipio_nonce}",
            use_container_width=True,
        )

    if add_clicked:
        if chosen == "—":
            st.sidebar.warning("Selecione um município antes de adicionar.")
        else:
            row = next((r for r in mun_options if r[2] == chosen), None)
            if row:
                _add_municipio_by_name(row[0], row[1], pncp_df, ibge_df)

    if st.session_state.selected_municipios:
        st.sidebar.caption("Selecionados:")
        keep = []
        for m in st.session_state.selected_municipios:
            c1, c2 = st.sidebar.columns([0.82, 0.18])
            with c1:
                ibge_show = f" | IBGE {m.get('codigo_ibge','')}" if _safe_text(m.get("codigo_ibge")) else ""
                st.markdown(f"- **{m['nome']}** / {m.get('uf','')} (`{m['codigo_pncp']}`{ibge_show})")
            with c2:
                if st.button("✕", key=f"rm_{m['codigo_pncp']}", help=f"Remover {m['nome']}"):
                    pass
                else:
                    keep.append(m)
        if len(keep) != len(st.session_state.selected_municipios):
            st.session_state.selected_municipios = keep
            st.rerun()

    st.sidebar.subheader("💾 Pesquisa salva")
    save_name = st.sidebar.text_input(
        "Nome da pesquisa",
        value=st.session_state.sidebar_inputs["save_name"],
        key="save_name_input",
    )
    col_s1, col_s2 = st.sidebar.columns(2)
    with col_s1:
        salvar = st.button("Salvar", use_container_width=True, key="btn_salvar")
    with col_s2:
        excluir = st.button("Excluir", use_container_width=True, key="btn_excluir")

    if salvar:
        nm = _safe_text(save_name)
        if not nm:
            st.sidebar.error("Informe um nome para salvar.")
        else:
            st.session_state.saved_searches[nm] = {
                "palavra_chave": palavra,
                "status_label": status_label,
                "uf": uf,
                "municipios": st.session_state.selected_municipios,
            }
            _persist_saved_searches(st.session_state.saved_searches)
            st.sidebar.success(f"Pesquisa '{nm}' salva.")

    if excluir:
        nm = _safe_text(save_name)
        if nm and nm in st.session_state.saved_searches:
            del st.session_state.saved_searches[nm]
            _persist_saved_searches(st.session_state.saved_searches)
            st.sidebar.success(f"Pesquisa '{nm}' excluída.")
        else:
            st.sidebar.error("Informe o nome exato de uma pesquisa salva para excluir.")

    st.sidebar.subheader("📚 Pesquisas salvas")
    nomes = sorted(list(st.session_state.saved_searches.keys()))
    selected_saved = st.sidebar.selectbox("Carregar pesquisa", ["—"] + nomes, index=0, key="select_saved")
    carregar = st.sidebar.button("Carregar", key="btn_carregar", use_container_width=True)

    if carregar and selected_saved != "—":
        payload = st.session_state.saved_searches.get(selected_saved, {})
        st.session_state.sidebar_inputs["palavra_chave"] = _safe_text(payload.get("palavra_chave"))
        saved_status = _safe_text(payload.get("status_label"))
        st.session_state.sidebar_inputs["status_label"] = saved_status if saved_status in STATUS_LABELS else STATUS_LABELS[0]
        st.session_state.sidebar_inputs["uf"] = _safe_text(payload.get("uf")) or UF_PLACEHOLDER
        st.session_state.uf_prev = st.session_state.sidebar_inputs["uf"]
        st.session_state.municipio_nonce += 1
        st.session_state.selected_municipios = _normalize_saved_municipios(
            payload.get("municipios", []),
            pncp_df,
            ibge_df,
        )
        st.session_state.sidebar_inputs["save_name"] = selected_saved
        st.sidebar.success(f"Pesquisa '{selected_saved}' carregada.")
        st.rerun()

    st.session_state.sidebar_inputs["palavra_chave"] = palavra
    st.session_state.sidebar_inputs["status_label"] = status_label
    st.session_state.sidebar_inputs["uf"] = uf
    st.session_state.sidebar_inputs["save_name"] = save_name
    st.session_state.sidebar_inputs["selected_saved"] = selected_saved

    disparar = st.sidebar.button("🔎 Pesquisar", use_container_width=True, type="primary", key="btn_pesquisar")
    if disparar and uf == UF_PLACEHOLDER:
        st.sidebar.error("Selecione uma UF para habilitar a pesquisa.")
        disparar = False
    return disparar


def _cb_prev(total_pages: int):
    st.session_state.card_page = max(1, int(st.session_state.get("card_page", 1)) - 1)


def _cb_next(total_pages: int):
    st.session_state.card_page = min(total_pages, int(st.session_state.get("card_page", 1)) + 1)


def _cb_page_size_change():
    st.session_state.card_page = 1


# ==========================
# UI
# ==========================
def main():
    st.title("📑 Acerte Licitações — O seu Buscador de Editais (Backup)")
    st.caption(
        "Versão de contingência com busca alternativa (API de Dados Abertos). "
        "Selecione UF e municípios, depois clique em Pesquisar."
    )

    st.markdown(
        """
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
          background: #f3f6fb;
          border: 1px solid #b9d0ee;
          border-radius: 16px;
          padding: 1.25rem 1.15rem;
          margin-bottom: 1rem;
          box-shadow: none;
        }
        .ac-card h3 {
          margin-top: 0;
          margin-bottom: 0.7rem;
          font-size: 1.85rem;
          font-weight: 700;
          color: #0b1b36;
          letter-spacing: -0.01em;
        }
        .ac-muted { color: #2b4677; font-size: 0.95rem; margin-bottom: 0.65rem; }
        .ac-obj { margin-top: 0.2rem; margin-bottom: 0.8rem; font-size: 1.04rem; color: #0b1b36; }
        .ac-meta { margin-top: 0.2rem; font-size: 1rem; color: #0b1b36; }
        .ac-actions { display:flex; justify-content:flex-end; margin-top: 0.9rem; }
        .ac-badge {
          background: #eaf1ff; border: 1px solid #bcd0f7; color: #2d62b3;
          padding: 0.18rem 0.5rem; border-radius: 999px; font-size: 0.82rem;
        }
        .ac-link {
          text-decoration:none; padding:0.54rem 1rem; border-radius:12px;
          border:1px solid #8db0ea; color:#2d62b3; background:#f2f7ff;
          font-weight: 600;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    _ensure_session_state()

    try:
        pncp_df = load_municipios_pncp()
    except Exception as e:
        st.error(f"Erro ao carregar 'ListaMunicipiosPNCP.csv': {e}")
        st.stop()

    ibge_df = load_ibge_catalog_local()

    disparar = _sidebar(pncp_df, ibge_df)

    # enriquece código IBGE para selecionados (inclui pesquisas antigas)
    for m in st.session_state.selected_municipios:
        cod_ibge = _safe_text(m.get("codigo_ibge"))
        if not cod_ibge:
            m["codigo_ibge"] = resolver_codigo_ibge(
                nome_municipio=_safe_text(m.get("nome")),
                uf=_safe_text(m.get("uf")),
                pncp_df=pncp_df,
                ibge_local_df=ibge_df,
                codigo_ibge_existente="",
            )

    status_value = STATUS_MAP.get(st.session_state.sidebar_inputs["status_label"], "")
    palavra_chave = _safe_text(st.session_state.sidebar_inputs["palavra_chave"])
    signature = {
        "municipios": [m["codigo_pncp"] for m in st.session_state.selected_municipios],
        "municipios_meta": [
            {
                "codigo_pncp": _safe_text(m.get("codigo_pncp")),
                "nome": _safe_text(m.get("nome")),
                "uf": _safe_text(m.get("uf")).upper(),
                "codigo_ibge": _safe_text(m.get("codigo_ibge")),
            }
            for m in st.session_state.selected_municipios
        ],
        "status": status_value,
        "q": palavra_chave.lower(),
    }

    if disparar:
        if not signature["municipios"]:
            st.warning("Selecione pelo menos um município para pesquisar.")
            st.stop()
        with st.spinner("Consultando API de Dados Abertos..."):
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
            st.warning("Filtros alterados após a última coleta. Clique em **Pesquisar** para atualizar.")

    st.subheader(f"Resultados ({len(df)})")
    if df.empty:
        st.info("Nenhum resultado encontrado com os critérios atuais.")
        st.caption(
            "Dica: confirme o município/UF selecionados e teste o status **Todos** para validar se há registros no período."
        )
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
        index=[10, 20, 50].index(st.session_state.get("page_size_cards", 10))
        if st.session_state.get("page_size_cards", 10) in [10, 20, 50]
        else 0,
        key="page_size_cards",
        on_change=_cb_page_size_change,
    )
    page_size_cards = int(st.session_state.get("page_size_cards", 10))
    total_items = len(df)
    total_pages = max(1, (total_items + page_size_cards - 1) // page_size_cards)

    col_a, col_b, col_c = st.columns([1, 2, 1])
    with col_a:
        st.button(
            "◀ Anterior",
            key="prev_top",
            disabled=(st.session_state.get("card_page", 1) <= 1),
            on_click=_cb_prev,
            kwargs={"total_pages": total_pages},
        )
    with col_c:
        st.button(
            "Próxima ▶",
            key="next_top",
            disabled=(st.session_state.get("card_page", 1) >= total_pages),
            on_click=_cb_next,
            kwargs={"total_pages": total_pages},
        )
    with col_b:
        st.markdown(f"**Página {st.session_state.get('card_page',1)} de {total_pages}**")

    start = (st.session_state.get("card_page", 1) - 1) * page_size_cards
    end = start + page_size_cards
    page_df = df.iloc[start:end].copy()

    for _, row in page_df.iterrows():
        uid = _uid_from_row(row)
        tr_flag = bool(st.session_state.tr_marks.get(uid, False))
        na_flag = bool(st.session_state.na_marks.get(uid, False))

        link = _safe_text(row.get("Link para o edital"))
        titulo = _safe_text(row.get("Título")) or "(Sem título)"
        cidade = _safe_text(row.get("Cidade"))
        uf = _safe_text(row.get("UF"))
        pub = _safe_text(row.get("Publicação"))
        fim = _safe_text(row.get("Fim do envio de proposta"))
        objeto = _safe_text(row.get("Objeto"))
        modalidade = _safe_text(row.get("Modalidade"))
        tipo = _safe_text(row.get("Tipo")) or _safe_text(row.get("Tipo (documento)")) or "Edital"
        orgao = _safe_text(row.get("Orgão"))

        html = f'''
        <div class="ac-card">
            <h3>{titulo}</h3>
            <div class="ac-muted">
                <span class="ac-badge">{cidade} / {uf}</span>
                &nbsp;•&nbsp;
                <strong>Publicação:</strong> {pub}
                &nbsp;|&nbsp;
                <strong>Fim do envio:</strong> {fim}
            </div>
            <div class="ac-obj"><strong>Objeto:</strong> {objeto}</div>
            <div class="ac-meta">
                <strong>Modalidade:</strong> {modalidade}
                &nbsp;&nbsp;
                <strong>Tipo:</strong> {tipo}
                &nbsp;&nbsp;
                <strong>Órgão:</strong> {orgao}
            </div>
            <div class="ac-actions">
                {f'<a class="ac-link" href="{link}" target="_blank">Abrir edital</a>' if link else ''}
            </div>
        </div>
        '''
        st.markdown(html, unsafe_allow_html=True)

        _, col_cb_tr, col_cb_na = st.columns([6, 1.3, 1.3])
        with col_cb_tr:
            new_tr = st.checkbox("TR Elaborado", value=tr_flag, key=f"tr_{uid}")
        with col_cb_na:
            new_na = st.checkbox("Não Atende", value=na_flag, key=f"na_{uid}")

        changed = False
        if new_tr != tr_flag:
            st.session_state.tr_marks[uid] = bool(new_tr)
            _persist_marks("tr_marks.json", TR_MARKS_PATHS, st.session_state.tr_marks)
            changed = True
        if new_na != na_flag:
            st.session_state.na_marks[uid] = bool(new_na)
            _persist_marks("na_marks.json", NA_MARKS_PATHS, st.session_state.na_marks)
            changed = True
        if changed:
            st.rerun()

    col_a2, col_b2, col_c2 = st.columns([1, 2, 1])
    with col_a2:
        st.button(
            "◀ Anterior",
            key="prev_bottom",
            disabled=(st.session_state.get("card_page", 1) <= 1),
            on_click=_cb_prev,
            kwargs={"total_pages": total_pages},
        )
    with col_c2:
        st.button(
            "Próxima ▶",
            key="next_bottom",
            disabled=(st.session_state.get("card_page", 1) >= total_pages),
            on_click=_cb_next,
            kwargs={"total_pages": total_pages},
        )
    with col_b2:
        st.markdown(f"**Página {st.session_state.get('card_page',1)} de {total_pages}**")

    st.divider()

    drop_cols = [c for c in ["_pub_raw", "_pub_dt", "_orgao_cnpj", "_ano", "_seq", "_id"] if c in df.columns]
    export_df = df.drop(columns=drop_cols, errors="ignore").copy()

    xlsx_buf = io.BytesIO()
    with pd.ExcelWriter(xlsx_buf, engine="openpyxl") as writer:
        export_df.to_excel(writer, index=False, sheet_name="PNCP")
    xlsx_bytes = xlsx_buf.getvalue()

    st.markdown("### ⬇️ Baixar planilha")
    st.download_button(
        "Baixar XLSX",
        data=xlsx_bytes,
        file_name=f"pncp_backup_resultados_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
        type="primary",
    )


if __name__ == "__main__":
    main()

