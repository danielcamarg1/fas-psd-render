import os
import re
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

FAS_KEY = (os.getenv("FAS_API_KEY", "") or "").strip()
BASE = "https://apps.fas.usda.gov/PSDOnlineDataServices/api"

# Cache simples em memória (evita chamar a lista toda a cada requisição)
_CACHE = {
    "commodities": None,
    "countries": None,
}

def call_fas(endpoint, params=None):
    if not FAS_KEY:
        return {"ok": False, "error": "FAS_API_KEY não configurada no Render."}, 500

    url = f"{BASE}/{endpoint.lstrip('/')}"
    headers = {"API_KEY": FAS_KEY, "User-Agent": "fas-psd-render/2.0"}

    try:
        r = requests.get(url, headers=headers, params=params or {}, timeout=60)
    except Exception as e:
        return {"ok": False, "error": "Falha de conexão com a API do FAS.", "details": str(e), "url": url}, 502

    try:
        data = r.json()
    except Exception:
        data = {"raw_text": (r.text or "")[:2000]}

    return {"ok": 200 <= r.status_code < 300, "status_code": r.status_code, "url": r.url, "data": data}, r.status_code


def normalize(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"\s+", " ", s)
    return s


def strip_nonletters(s: str) -> str:
    return re.sub(r"[^a-z0-9\s]", "", normalize(s))


# Apelidos PT -> EN para commodities (expandimos depois: algodão, proteínas etc.)
PT_COMMODITY_ALIASES = {
    "soja": ["soybeans", "soybean", "soy"],
    "milho": ["corn", "maize"],
    "arroz": ["rice"],
    "algodao": ["cotton"],
    "algodão": ["cotton"],
    "cafe": ["coffee"],
    "café": ["coffee"],
    "acucar": ["sugar"],
    "açúcar": ["sugar"],
}

# Apelidos PT -> EN para países (expandimos conforme uso)
PT_COUNTRY_ALIASES = {
    "brasil": "brazil",
    "eua": "united states",
    "estados unidos": "united states",
    "reino unido": "united kingdom",
    "inglaterra": "united kingdom",
    "alemanha": "germany",
    "franca": "france",
    "frança": "france",
    "espanha": "spain",
    "italia": "italy",
    "itália": "italy",
    "mexico": "mexico",
    "méxico": "mexico",
    "argentina": "argentina",
    "paraguai": "paraguay",
    "uruguai": "uruguay",
    "china": "china",
    "india": "india",
    "índia": "india",
    "russia": "russia",
    "rússia": "russia",
}


# Apelidos amigáveis para métricas (você pode pedir em PT)
# (a API usa "AttributeDescription" em inglês)
METRIC_ALIASES = {
    "producao": "Production",
    "produção": "Production",
    "production": "Production",

    "consumo": "Domestic Consumption",
    "consumo domestico": "Domestic Consumption",
    "domestic consumption": "Domestic Consumption",

    "importacao": "MY Imports",
    "importação": "MY Imports",
    "imports": "MY Imports",
    "my imports": "MY Imports",
    "ty imports": "TY Imports",

    "exportacao": "MY Exports",
    "exportação": "MY Exports",
    "exports": "MY Exports",
    "my exports": "MY Exports",
    "ty exports": "TY Exports",

    "estoque inicial": "Beginning Stocks",
    "beginning stocks": "Beginning Stocks",

    "estoque final": "Ending Stocks",
    "ending stocks": "Ending Stocks",

    "oferta total": "Total Supply",
    "total supply": "Total Supply",
}


def metric_canonical(metric: str) -> str:
    m = normalize(metric)
    return METRIC_ALIASES.get(m, metric)  # se já vier em inglês correto, passa direto


def commodity_display(it: dict) -> str:
    return (it.get("CommodityName") or it.get("Name") or it.get("CommodityDescription")
            or it.get("Description") or "").strip()


def commodity_code(it: dict) -> str:
    return (it.get("CommodityCode") or it.get("Code") or it.get("Id") or "").strip()


def score_commodity(name: str, query_norm: str) -> int:
    """
    Escolha de commodity:
    - Para "soja": preferir grão ("Oilseed, Soybean" ou "Soybeans") e evitar meal/oil.
    """
    n = normalize(name)
    score = 0

    # Match forte
    if query_norm and query_norm in n:
        score += 10

    if query_norm in ["soja", "soybeans", "soybean", "soy"]:
        # preferir "Oilseed, Soybean" e "Soybeans"
        if "oilseed" in n and "soybean" in n:
            score += 250
        if n.strip() == "soybeans" or n.startswith("soybeans"):
            score += 200
        if "meal" in n:
            score -= 200
        if "oil," in n or n.startswith("oil "):
            score -= 120
        if "oil, soybean" in n:
            score -= 120

    # nomes mais curtos tendem a ser commodity-base
    score -= max(0, len(n) - 24) // 5
    return score


def resolve_commodity(commodities_list, user_input: str):
    raw = (user_input or "").strip()
    # se vier um código numérico direto
    if re.fullmatch(r"\d{5,8}", raw):
        return raw, None

    key = normalize(raw)
    aliases = PT_COMMODITY_ALIASES.get(key, [raw])

    candidates = []
    for a in aliases:
        a_norm = normalize(a)
        a_clean = strip_nonletters(a)

        for it in commodities_list:
            if not isinstance(it, dict):
                continue
            nm = commodity_display(it)
            code = commodity_code(it)
            if not nm or not code:
                continue

            nm_norm = normalize(nm)
            nm_clean = strip_nonletters(nm)

            if (a_norm and a_norm in nm_norm) or (a_clean and a_clean in nm_clean) or (a_norm and a_norm == nm_norm):
                candidates.append((nm, code))

    if not candidates:
        return None, None

    # escolher melhor candidato
    best_code, best_name = None, None
    best_score = -10**9
    for nm, code in candidates:
        sc = score_commodity(nm, key)
        if sc > best_score:
            best_score = sc
            best_code, best_name = code, nm

    return best_code, best_name


def resolve_country(countries_list, user_input: str):
    raw = (user_input or "").strip()
    translated = PT_COUNTRY_ALIASES.get(normalize(raw), raw)

    t_norm = normalize(translated)
    t_clean = strip_nonletters(translated)

    for it in countries_list:
        if not isinstance(it, dict):
            continue
        nm = (it.get("CountryName") or it.get("Name") or it.get("Description") or "").strip()
        code = (it.get("CountryCode") or it.get("Code") or it.get("Id") or "").strip()
        if not nm or not code:
            continue
        nm_norm = normalize(nm)
        nm_clean = strip_nonletters(nm)

        if (t_norm and t_norm == nm_norm) or (t_norm and t_norm in nm_norm) or (t_clean and t_clean in nm_clean):
            return code, nm

    return None, None


def pick_world_code(countries_list):
    # tenta achar uma linha oficial "World"
    for name_try in ["World", "world", "WORLD"]:
        code, nm = resolve_country(countries_list, name_try)
        if code:
            return code, nm
    return None, None


def fetch_commodities():
    if _CACHE["commodities"] is not None:
        return _CACHE["commodities"], None

    env, st = call_fas("LookupData/GetCommodities")
    if st != 200 or not env.get("ok"):
        return None, {"error": "Falha ao buscar commodities", "details": env}

    _CACHE["commodities"] = env.get("data", [])
    return _CACHE["commodities"], None


def fetch_countries():
    if _CACHE["countries"] is not None:
        return _CACHE["countries"], None

    env, st = call_fas("LookupData/GetCountries")
    if st != 200 or not env.get("ok"):
        return None, {"error": "Falha ao buscar países", "details": env}

    _CACHE["countries"] = env.get("data", [])
    return _CACHE["countries"], None


def filter_to_balance_sheet(rows):
    wanted = {
        "Production",
        "Domestic Consumption",
        "MY Imports",
        "TY Imports",
        "MY Exports",
        "TY Exports",
        "Beginning Stocks",
        "Ending Stocks",
        "Total Supply",
    }
    out = []
    for r in rows:
        ad = (r.get("AttributeDescription") or "").strip()
        if ad in wanted:
            out.append(r)
    return out


def summarize(rows):
    summary = {}
    units = {}
    meta = {}

    for r in rows:
        k = (r.get("AttributeDescription") or "").strip()
        if not k:
            continue
        summary[k] = r.get("Value")
        units[k] = (r.get("UnitDescription") or "").strip()
        meta = {
            "CommodityDescription": (r.get("CommodityDescription") or "").strip(),
            "CountryName": (r.get("CountryName") or "").strip(),
            "MarketYear": r.get("MarketYear"),
            "Month": r.get("Month"),
            "CalendarYear": r.get("CalendarYear"),
        }

    return summary, units, meta


def sum_across_countries(rows):
    acc = {}
    units = {}
    meta = {"CountryName": "World (computed sum)"}

    for r in rows:
        k = (r.get("AttributeDescription") or "").strip()
        if not k:
            continue
        v = r.get("Value")
        if isinstance(v, (int, float)):
            acc[k] = acc.get(k, 0) + v
        if k not in units:
            units[k] = (r.get("UnitDescription") or "").strip()

        if "CommodityDescription" not in meta and r.get("CommodityDescription"):
            meta["CommodityDescription"] = (r.get("CommodityDescription") or "").strip()
        if "MarketYear" not in meta and r.get("MarketYear"):
            meta["MarketYear"] = r.get("MarketYear")
        if "Month" not in meta and r.get("Month"):
            meta["Month"] = r.get("Month")
        if "CalendarYear" not in meta and r.get("CalendarYear"):
            meta["CalendarYear"] = r.get("CalendarYear")

    return acc, units, meta


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "ok": True,
        "endpoints": {
            "psd": "/psd?commodity=soja&country=brasil&year=2024",
            "top": "/top?commodity=milho&year=2024&metric=producao&n=15",
            "findCommodity": "/findCommodity?name=soja"
        }
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "fas_key_configured": bool(FAS_KEY), "base": BASE})


@app.route("/findCommodity", methods=["GET"])
def find_commodity():
    name = request.args.get("name", "")

    commodities_list, err = fetch_commodities()
    if err:
        return jsonify(err), 502

    code, found_name = resolve_commodity(commodities_list, name)
    return jsonify({"input": name, "chosen_code": code, "chosen_name": found_name}), 200


@app.route("/psd", methods=["GET"])
def psd():
    commodity_name = request.args.get("commodity", "")
    country_name = request.args.get("country", "mundo")
    year = request.args.get("year", "")

    if not year or not commodity_name:
        return jsonify({"error": "Use /psd?commodity=soja&country=brasil&year=2024"}), 400

    commodities_list, err = fetch_commodities()
    if err:
        return jsonify(err), 502

    commodity_code, commodity_found = resolve_commodity(commodities_list, commodity_name)
    if not commodity_code:
        return jsonify({"error": f"Commodity não encontrada: {commodity_name}"}), 404

    d_env, st = call_fas("CommodityData/GetCommodityDataByYear", params={"CommodityCode": commodity_code, "marketYear": int(year)})
    if st != 200 or not d_env.get("ok"):
        return jsonify({"error": "Falha ao buscar dados", "details": d_env}), 502

    rows = d_env.get("data", [])
    if not isinstance(rows, list) or not rows:
        return jsonify({"error": "Sem dados retornados para esse ano/commodity."}), 404

    rows = filter_to_balance_sheet(rows)

    is_world = normalize(country_name) in ["world", "mundo", "global", "all"]

    # mundo
    if is_world:
        countries_list, err2 = fetch_countries()
        if err2:
            # fallback: soma tudo
            summary, units, meta = sum_across_countries(rows)
            return jsonify({
                "request": {"commodity": commodity_name, "country": country_name, "year": year},
                "resolved": {"CommodityCode": commodity_code, "CommodityName_found": commodity_found, "CountryScope": "World (computed sum)"},
                "meta": meta,
                "balance_sheet": summary,
                "units": units
            }), 200

        world_code, world_name = pick_world_code(countries_list)
        if world_code:
            world_rows = [r for r in rows if (r.get("CountryCode") or "").strip() == world_code]
            if world_rows:
                summary, units, meta = summarize(world_rows)
                if world_name:
                    meta["CountryName"] = world_name
                return jsonify({
                    "request": {"commodity": commodity_name, "country": country_name, "year": year},
                    "resolved": {"CommodityCode": commodity_code, "CommodityName_found": commodity_found, "CountryScope": "World (official row)"},
                    "meta": meta,
                    "balance_sheet": summary,
                    "units": units
                }), 200

        summary, units, meta = sum_across_countries(rows)
        return jsonify({
            "request": {"commodity": commodity_name, "country": country_name, "year": year},
            "resolved": {"CommodityCode": commodity_code, "CommodityName_found": commodity_found, "CountryScope": "World (computed sum)"},
            "meta": meta,
            "balance_sheet": summary,
            "units": units
        }), 200

    # país
    countries_list, err2 = fetch_countries()
    if err2:
        return jsonify(err2), 502

    country_code, found_country = resolve_country(countries_list, country_name)
    if not country_code:
        return jsonify({"error": f"País não encontrado: {country_name}"}), 404

    rows = [r for r in rows if (r.get("CountryCode") or "").strip() == country_code]
    if not rows:
        return jsonify({"error": f"Sem dados para o país: {found_country} nesse ano."}), 404

    summary, units, meta = summarize(rows)
    return jsonify({
        "request": {"commodity": commodity_name, "country": country_name, "year": year},
        "resolved": {"CommodityCode": commodity_code, "CommodityName_found": commodity_found, "CountryCode": country_code, "CountryName_found": found_country},
        "meta": meta,
        "balance_sheet": summary,
        "units": units
    }), 200


@app.route("/top", methods=["GET"])
def top():
    """
    Top N países por métrica.

    Exemplo:
      /top?commodity=milho&year=2024&metric=producao&n=15
      /top?commodity=soja&year=2024&metric=exportacao&n=20
    """
    commodity_name = request.args.get("commodity", "")
    year = request.args.get("year", "")
    metric_in = request.args.get("metric", "Production")
    n = request.args.get("n", "15")

    if not commodity_name or not year:
        return jsonify({"error": "Use /top?commodity=milho&year=2024&metric=producao&n=15"}), 400

    try:
        year_i = int(year)
    except Exception:
        return jsonify({"error": "year precisa ser número (ex.: 2024)."}), 400

    try:
        n_i = int(n)
        n_i = max(1, min(n_i, 60))  # limita para não virar gigante
    except Exception:
        return jsonify({"error": "n precisa ser número (ex.: 15)."}), 400

    metric = metric_canonical(metric_in)

    commodities_list, err = fetch_commodities()
    if err:
        return jsonify(err), 502

    commodity_code, commodity_found = resolve_commodity(commodities_list, commodity_name)
    if not commodity_code:
        return jsonify({"error": f"Commodity não encontrada: {commodity_name}"}), 404

    d_env, st = call_fas("CommodityData/GetCommodityDataByYear", params={"CommodityCode": commodity_code, "marketYear": year_i})
    if st != 200 or not d_env.get("ok"):
        return jsonify({"error": "Falha ao buscar dados", "details": d_env}), 502

    rows = d_env.get("data", [])
    if not isinstance(rows, list) or not rows:
        return jsonify({"error": "Sem dados retornados para esse ano/commodity."}), 404

    # filtra só a métrica desejada
    metric_rows = []
    unit = None
    month = None
    cal_year = None
    comm_desc = None

    for r in rows:
        ad = (r.get("AttributeDescription") or "").strip()
        if ad != metric:
            continue

        val = r.get("Value")
        # só aceita numérico
        if not isinstance(val, (int, float)):
            continue

        ccode = (r.get("CountryCode") or "").strip()
        cname = (r.get("CountryName") or "").strip()

        # ignora "World" se existir para não aparecer no top
        if normalize(cname) in ["world"]:
            continue

        metric_rows.append({
            "countryCode": ccode,
            "countryName": cname,
            "value": val
        })

        if unit is None:
            unit = (r.get("UnitDescription") or "").strip()
        if month is None:
            month = (r.get("Month") or "").strip()
        if cal_year is None:
            cal_year = (r.get("CalendarYear") or "").strip()
        if comm_desc is None:
            comm_desc = (r.get("CommodityDescription") or "").strip()

    if not metric_rows:
        return jsonify({
            "error": "Sem linhas para essa métrica.",
            "hint": "Verifique o nome da métrica (ex.: Production, MY Exports, Ending Stocks).",
            "metric_used": metric
        }), 404

    # ordena desc e pega top N
    metric_rows.sort(key=lambda x: x["value"], reverse=True)
    top_rows = metric_rows[:n_i]

    return jsonify({
        "request": {"commodity": commodity_name, "year": year_i, "metric": metric_in, "n": n_i},
        "resolved": {"CommodityCode": commodity_code, "CommodityName_found": commodity_found, "metric_used": metric},
        "meta": {
            "CommodityDescription": comm_desc,
            "MarketYear": str(year_i),
            "Month": month,
            "CalendarYear": cal_year
        },
        "unit": unit,
        "top": top_rows
    }), 200
