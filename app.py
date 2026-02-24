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
    headers = {"API_KEY": FAS_KEY, "User-Agent": "fas-psd-render/1.5"}

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


PT_COMMODITY_ALIASES = {
    "soja": ["soybeans", "soybean", "soy"],
    "milho": ["corn", "maize"],
    "arroz": ["rice"],
    "cafe": ["coffee"],
    "café": ["coffee"],
    "acucar": ["sugar"],
    "açúcar": ["sugar"],
}

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
}


def commodity_display(it: dict) -> str:
    return (it.get("CommodityName") or it.get("Name") or it.get("CommodityDescription") or it.get("Description") or "").strip()


def commodity_code(it: dict) -> str:
    return (it.get("CommodityCode") or it.get("Code") or it.get("Id") or "").strip()


def score_commodity(name: str, query_norm: str) -> int:
    """
    Escolhe melhor commodity.
    Para 'soja': preferir grão (Soybeans) e evitar Meal/Oil.
    """
    n = normalize(name)
    score = 0

    # bônus por match exato/forte
    if query_norm and query_norm in n:
        score += 10

    if query_norm in ["soja", "soybeans", "soybean", "soy"]:
        # Queremos grão
        if n.strip() == "soybeans" or n.startswith("soybeans"):
            score += 200
        if "meal" in n:
            score -= 120
        if "oil" in n:
            score -= 120
        # penaliza derivados
        if "meal," in n or "oil," in n:
            score -= 50

    # preferir nomes mais curtos (geralmente mais “commodity base”)
    score -= max(0, len(n) - 20) // 5
    return score


def resolve_commodity(commodities_list, user_input: str):
    raw = (user_input or "").strip()
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

            # candidato se "parece" com o que queremos
            if (a_norm and a_norm in nm_norm) or (a_clean and a_clean in nm_clean) or (a_norm and a_norm == nm_norm):
                candidates.append((it, nm, code, a_norm))

    if not candidates:
        return None, None

    best = None
    best_score = -10**9
    # o query_norm usado para score é o "key" original (soja, milho etc.)
    query_norm = key
    for it, nm, code, a_norm in candidates:
        sc = score_commodity(nm, query_norm)
        # bônus extra se o alias principal bate
        if a_norm and a_norm == normalize(nm):
            sc += 20
        if sc > best_score:
            best_score = sc
            best = (code, nm)

    return best[0], best[1]


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
    for name_try in ["World", "world", "WORLD"]:
        code, nm = resolve_country(countries_list, name_try)
        if code:
            return code, nm
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
            "/findCommodity?name=soja",
            "/psd?commodity=soja&country=brasil&year=2024",
            "/psd?commodity=soja&country=mundo&year=2024",
            "/psd?commodity=milho&country=mundo&year=2024",
        ]
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "fas_key_configured": bool(FAS_KEY), "base": BASE})


@app.route("/findCommodity", methods=["GET"])
def find_commodity():
    name = request.args.get("name", "")
    c_env, st = call_fas("LookupData/GetCommodities")
    if st != 200 or not c_env.get("ok"):
        return jsonify({"error": "Falha ao buscar commodities", "details": c_env}), 502

    code, found_name = resolve_commodity(c_env.get("data", []), name)
    return jsonify({"input": name, "chosen_code": code, "chosen_name": found_name}), 200


@app.route("/psd", methods=["GET"])
def psd():
    commodity_name = request.args.get("commodity", "")
    country_name = request.args.get("country", "mundo")
    year = request.args.get("year", "")

    if not year or not commodity_name:
        return jsonify({"error": "Use /psd?commodity=soja&country=brasil&year=2024"}), 400

    # 1) resolve commodity code
    c_env, st = call_fas("LookupData/GetCommodities")
    if st != 200 or not c_env.get("ok"):
        return jsonify({"error": "Falha ao buscar commodities", "details": c_env}), 502

    commodity_code, commodity_found = resolve_commodity(c_env.get("data", []), commodity_name)
    if not commodity_code:
        return jsonify({"error": f"Commodity não encontrada: {commodity_name}"}), 404

    # 2) fetch year data
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
        p_env, st2 = call_fas("LookupData/GetCountries")
        if st2 == 200 and p_env.get("ok"):
            world_code, world_name = pick_world_code(p_env.get("data", []))
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
    p_env, st2 = call_fas("LookupData/GetCountries")
    if st2 != 200 or not p_env.get("ok"):
        return jsonify({"error": "Falha ao buscar países", "details": p_env}), 502

    country_code, found_country = resolve_country(p_env.get("data", []), country_name)
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
