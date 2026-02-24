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
    headers = {"API_KEY": FAS_KEY, "User-Agent": "fas-psd-render/1.4"}

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


# ✅ Atalhos FIXOS (para não cair no produto errado)
# Ajuste aqui conforme você for adicionando commodities.
FORCED_COMMODITY_CODES = {
    # soja = grão
    "soja": "0811000",        # Soybeans (beans)
    "soybeans": "0811000",
    "soybean": "0811000",
    # milho
    "milho": "0440000",       # Corn
    "corn": "0440000",
    # arroz
    "arroz": None,
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


def find_best_match(items, wanted, keys):
    w = normalize(wanted)
    w2 = strip_nonletters(wanted)

    if not w or not isinstance(items, list):
        return None

    for it in items:
        if not isinstance(it, dict):
            continue
        for k in keys:
            v = it.get(k)
            if isinstance(v, str) and w in normalize(v):
                return it

    for it in items:
        if not isinstance(it, dict):
            continue
        for k in keys:
            v = it.get(k)
            if isinstance(v, str) and w == normalize(v):
                return it

    for it in items:
        if not isinstance(it, dict):
            continue
        for k in keys:
            v = it.get(k)
            if isinstance(v, str) and w2 and w2 in strip_nonletters(v):
                return it

    return None


def resolve_country_code(countries_list, country_name):
    raw = (country_name or "").strip()
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

    # ✅ FORÇA commodity code quando existir no nosso mapa
    forced_code = FORCED_COMMODITY_CODES.get(normalize(commodity_name))
    if forced_code:
        commodity_code = forced_code
        commodity_found_name = None
    else:
        # fallback: tenta achar pelo catálogo
        c_env, st = call_fas("LookupData/GetCommodities")
        if st != 200 or not c_env.get("ok"):
            return jsonify({"error": "Falha ao buscar commodities", "details": c_env}), 502

        commodities_list = c_env.get("data", [])
        hit = find_best_match(
            commodities_list,
            commodity_name,
            keys=["CommodityName", "Name", "CommodityDescription", "Description"]
        )
        if not hit:
            return jsonify({"error": f"Commodity não encontrada: {commodity_name}"}), 404

        commodity_code = hit.get("CommodityCode") or hit.get("Code") or hit.get("Id")
        commodity_found_name = (hit.get("CommodityName") or hit.get("Name") or "").strip()

    # dados do ano
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
            countries_list = p_env.get("data", [])
            world_code, world_name = pick_world_code(countries_list)
            if world_code:
                world_rows = [r for r in rows if (r.get("CountryCode") or "").strip() == world_code]
                if world_rows:
                    summary, units, meta = summarize(world_rows)
                    if world_name:
                        meta["CountryName"] = world_name
                    return jsonify({
                        "request": {"commodity": commodity_name, "country": country_name, "year": year},
                        "resolved": {
                            "CommodityCode": commodity_code,
                            "CommodityName_found": commodity_found_name,
                            "CountryScope": "World (official row)"
                        },
                        "meta": meta,
                        "balance_sheet": summary,
                        "units": units
                    }), 200

        summary, units, meta = sum_across_countries(rows)
        return jsonify({
            "request": {"commodity": commodity_name, "country": country_name, "year": year},
            "resolved": {
                "CommodityCode": commodity_code,
                "CommodityName_found": commodity_found_name,
                "CountryScope": "World (computed sum)"
            },
            "meta": meta,
            "balance_sheet": summary,
            "units": units
        }), 200

    # país
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
            "CommodityName_found": commodity_found_name,
            "CountryCode": country_code,
            "CountryName_found": found_country_name
        },
        "meta": meta,
        "balance_sheet": summary,
        "units": units
    }), 200
