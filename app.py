import os
import re
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

FAS_KEY = (os.getenv("FAS_API_KEY", "") or "").strip()
BASE = "https://apps.fas.usda.gov/PSDOnlineDataServices/api"


def call_fas(endpoint, params=None):
    if not FAS_KEY:
        return {"ok": False, "error": "FAS_API_KEY não configurada no Render."}, 500

    url = f"{BASE}/{endpoint.lstrip('/')}"
    headers = {"API_KEY": FAS_KEY, "User-Agent": "fas-psd-render/1.3"}

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


# Commodity: PT -> tentativas EN (mas vamos escolher de forma inteligente)
PT_COMMODITY_ALIASES = {
    "soja": ["soybeans", "soybean"],
    "milho": ["corn", "maize"],
    "arroz": ["rice"],
    "cafe": ["coffee"],
    "café": ["coffee"],
    "acucar": ["sugar"],
    "açúcar": ["sugar"],
}

# País: PT -> EN mais comum (pode expandir depois)
PT_COUNTRY_ALIASES = {
    "brasil": "brazil",
    "eua": "united states",
    "estados unidos": "united states",
    "reino unido": "united kingdom",
    "inglaterra": "united kingdom",
    "russia": "russia",
    "rússia": "russia",
    "china": "china",
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
}


def find_best_match(items, wanted, keys):
    """
    Procura por:
    1) contém (normalizado)
    2) igual (normalizado)
    3) contém (sem caracteres especiais)
    """
    w = normalize(wanted)
    w2 = strip_nonletters(wanted)

    if not w or not isinstance(items, list):
        return None

    # contém (normal)
    for it in items:
        if not isinstance(it, dict):
            continue
        for k in keys:
            v = it.get(k)
            if isinstance(v, str) and w in normalize(v):
                return it

    # igual (normal)
    for it in items:
        if not isinstance(it, dict):
            continue
        for k in keys:
            v = it.get(k)
            if isinstance(v, str) and w == normalize(v):
                return it

    # contém (limpo)
    for it in items:
        if not isinstance(it, dict):
            continue
        for k in keys:
            v = it.get(k)
            if isinstance(v, str) and w2 and w2 in strip_nonletters(v):
                return it

    return None


def score_commodity(name: str, target: str) -> int:
    """
    Ajuda a escolher a commodity certa quando existem várias parecidas.
    Quanto maior o score, melhor.

    Regras principais:
    - Se o target for 'soybeans/soja': preferir 'Soybeans' e evitar 'Meal, Soybean' e 'Oil, Soybean'
    """
    n = normalize(name)

    # Caso especial: SOJA -> grão
    if target in ["soja", "soybeans", "soybean"]:
        score = 0
        if "soybeans" in n or n.strip() == "soybeans":
            score += 100
        if "meal" in n:
            score -= 80
        if "oil" in n:
            score -= 80
        if "seed" in n and "soy" in n:
            score -= 30
        return score

    return 0


def resolve_commodity_code(commodities_list, commodity_name):
    """
    Resolve commodity code por nome, com preferência inteligente.
    Aceita código direto também.
    """
    raw = (commodity_name or "").strip()

    # Se já veio um código numérico
    if re.fullmatch(r"\d{5,8}", raw):
        return raw, {"CommodityName_found": None}

    base_key = normalize(raw)
    aliases = PT_COMMODITY_ALIASES.get(base_key, [raw])

    # Coletar candidatos
    candidates = []
    for name_try in aliases + [raw]:
        # em vez de "primeiro que acha", vamos coletar todos os matches "contém"
        w = normalize(name_try)
        w2 = strip_nonletters(name_try)

        for it in commodities_list:
            if not isinstance(it, dict):
                continue
            nm = (it.get("CommodityName") or it.get("Name") or it.get("CommodityDescription") or "").strip()
            if not nm:
                continue
            nm_n = normalize(nm)
            nm_c = strip_nonletters(nm)

            if (w and w in nm_n) or (w2 and w2 in nm_c) or (w and w == nm_n):
                code = it.get("CommodityCode") or it.get("Code") or it.get("Id")
                if code:
                    candidates.append((it, nm, code))

    if not candidates:
        return None, None

    # Escolher melhor candidato por score
    best = None
    best_score = -10**9
    for it, nm, code in candidates:
        sc = score_commodity(nm, base_key)
        # bônus para match exato do nome (quando acontecer)
        if normalize(nm) == normalize(aliases[0]):
            sc += 10
        if sc > best_score:
            best_score = sc
            best = (it, nm, code)

    it, nm, code = best
    return code, {"CommodityName_found": nm}


def resolve_country_code(countries_list, country_name):
    """
    Resolve país aceitando PT -> EN.
    """
    raw = (country_name or "").strip()
    if not raw:
        return None, None

    # traduz PT->EN quando existir
    translated = PT_COUNTRY_ALIASES.get(normalize(raw), raw)

    hit = find_best_match(
        countries_list,
        translated,
        keys=["CountryName", "Name", "CountryDescription", "Description"]
    )
    if not hit:
        return None, None

    code = hit.get("CountryCode") or hit.get("Code") or hit.get("Id")
    found_name = (hit.get("CountryName") or hit.get("Name") or "").strip()
    return code, found_name


def pick_world_code(countries_list):
    for name_try in ["world", "World", "WORLD"]:
        code, found = resolve_country_code(countries_list, name_try)
        if code:
            return code, found
    # fallback contém
    hit = find_best_match(countries_list, "world", ["CountryName", "Name"])
    if hit:
        code = hit.get("CountryCode") or hit.get("Code") or hit.get("Id")
        found = (hit.get("CountryName") or hit.get("Name") or "").strip()
        if code:
            return code, found
    return None, None


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
        "use": [
            "/health",
            "/psd?commodity=soja&country=brasil&year=2024",
            "/psd?commodity=soja&country=mundo&year=2024",
            "/psd?commodity=milho&country=mundo&year=2024",
        ]
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "fas_key_configured": bool(FAS_KEY), "base": BASE})


@app.route("/psd", methods=["GET"])
def psd():
    commodity_name = request.args.get("commodity", "")
    country_name = request.args.get("country", "mundo")
    year = request.args.get("year", "")

    if not year or not commodity_name:
        return jsonify({"error": "Use /psd?commodity=soja&country=brasil&year=2024"}), 400

    # 1) buscar commodities e resolver código (com preferência por 'Soybeans' no caso de soja)
    c_env, st = call_fas("LookupData/GetCommodities")
    if st != 200 or not c_env.get("ok"):
        return jsonify({"error": "Falha ao buscar commodities", "details": c_env}), 502

    commodities_list = c_env.get("data", [])
    commodity_code, resolved_commodity = resolve_commodity_code(commodities_list, commodity_name)
    if not commodity_code:
        return jsonify({"error": f"Commodity não encontrada: {commodity_name}"}), 404

    # 2) baixar dados do ano (vem muitos países) e filtrar depois
    d_env, st = call_fas("CommodityData/GetCommodityDataByYear", params={"CommodityCode": commodity_code, "marketYear": int(year)})
    if st != 200 or not d_env.get("ok"):
        return jsonify({"error": "Falha ao buscar dados", "details": d_env}), 502

    rows = d_env.get("data", [])
    if not isinstance(rows, list) or not rows:
        return jsonify({"error": "Sem dados retornados para esse ano/commodity."}), 404

    # 3) manter só itens do balanço
    rows = filter_to_balance_sheet(rows)

    # 4) filtrar país / mundo
    is_world = normalize(country_name) in ["world", "mundo", "global", "all"]

    if is_world:
        # tenta filtrar a linha oficial "World"
        p_env, st2 = call_fas("LookupData/GetCountries")
        if st2 == 200 and p_env.get("ok"):
            countries_list = p_env.get("data", [])
            world_code, world_name = pick_world_code(countries_list)
            if world_code:
                world_rows = [r for r in rows if (r.get("CountryCode") or "").strip() == world_code]
                if world_rows:
                    summary, units, meta = summarize(world_rows)
                    meta["CountryName"] = world_name or meta.get("CountryName") or "World"
                    return jsonify({
                        "request": {"commodity": commodity_name, "country": country_name, "year": year},
                        "resolved": {
                            "CommodityCode": commodity_code,
                            **(resolved_commodity or {}),
                            "CountryScope": "World (official row)"
                        },
                        "meta": meta,
                        "balance_sheet": summary,
                        "units": units
                    }), 200

        # fallback: soma tudo
        summary, units, meta = sum_across_countries(rows)
        return jsonify({
            "request": {"commodity": commodity_name, "country": country_name, "year": year},
            "resolved": {
                "CommodityCode": commodity_code,
                **(resolved_commodity or {}),
                "CountryScope": "World (computed sum)"
            },
            "meta": meta,
            "balance_sheet": summary,
            "units": units
        }), 200

    # país normal (PT->EN automático)
    p_env, st2 = call_fas("LookupData/GetCountries")
    if st2 != 200 or not p_env.get("ok"):
        return jsonify({"error": "Falha ao buscar países", "details": p_env}), 502

    countries_list = p_env.get("data", [])
    country_code, found_country_name = resolve_country_code(countries_list, country_name)
    if not country_code:
        return jsonify({"error": f"País não encontrado: {country_name}"}), 404

    rows = [r for r in rows if (r.get("CountryCode") or "").strip() == country_code]
    if not rows:
        return jsonify({"error": f"Sem dados para o país: {found_country_name} nesse ano."}), 404

    summary, units, meta = summarize(rows)
    return jsonify({
        "request": {"commodity": commodity_name, "country": country_name, "year": year},
        "resolved": {
            "CommodityCode": commodity_code,
            **(resolved_commodity or {}),
            "CountryCode": country_code,
            "CountryName_found": found_country_name
        },
        "meta": meta,
        "balance_sheet": summary,
        "units": units
    }), 200
