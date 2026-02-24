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
    headers = {"API_KEY": FAS_KEY, "User-Agent": "fas-psd-render/1.1"}

    try:
        r = requests.get(url, headers=headers, params=params or {}, timeout=45)
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


# “Tradução” rápida PT -> nomes que existem na lista do USDA
PT_TO_EN = {
    "soja": "soybeans",
    "milho": "corn",
    "arroz": "rice",
    "cafe": "coffee",
    "café": "coffee",
    "acucar": "sugar",
    "açúcar": "sugar",
}


def translate_commodity_name(name: str) -> str:
    n = normalize(name)
    return PT_TO_EN.get(n, name)


def find_best_match(items, wanted, keys):
    w = normalize(wanted)
    if not w or not isinstance(items, list):
        return None

    # contém
    for it in items:
        if not isinstance(it, dict):
            continue
        for k in keys:
            v = it.get(k)
            if isinstance(v, str) and w in normalize(v):
                return it

    # igual
    for it in items:
        if not isinstance(it, dict):
            continue
        for k in keys:
            v = it.get(k)
            if isinstance(v, str) and w == normalize(v):
                return it

    return None


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "ok": True,
        "use": [
            "/health",
            "/_routes",
            "/commodities",
            "/countries",
            "/psd?commodity=soja&country=brasil&year=2024",
            "/psd?commodity=milho&country=mundo&year=2024",
            "/psd?commodity=rice&country=india&year=2024"
        ]
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "fas_key_configured": bool(FAS_KEY), "base": BASE})


@app.route("/_routes", methods=["GET"])
def routes():
    return jsonify(sorted([str(r) for r in app.url_map.iter_rules()]))


@app.route("/commodities", methods=["GET"])
def commodities():
    env, st = call_fas("LookupData/GetCommodities")
    return jsonify(env), st


@app.route("/countries", methods=["GET"])
def countries():
    env, st = call_fas("LookupData/GetCountries")
    return jsonify(env), st


def filter_to_balance_sheet(rows):
    """
    Mantém só os itens mais importantes para “balanço”:
    produção, consumo, importação, exportação, estoque final.
    (Os nomes vêm da própria API.)
    """
    wanted = {
        "Production",
        "TY Exports",
        "MY Exports",
        "TY Imports",
        "MY Imports",
        "Domestic Consumption",
        "Feed Dom. Consumption",
        "Food Use Dom. Consumption",
        "Industrial Dom. Consumption",
        "Ending Stocks",
        "Beginning Stocks",
    }
    out = []
    for r in rows:
        ad = (r.get("AttributeDescription") or "").strip()
        if ad in wanted:
            out.append(r)
    return out


def summarize(rows):
    """
    Converte a lista de linhas em uma tabelinha simples:
    { "Production": 123, "TY Exports": 45, ... }
    """
    summary = {}
    unit = {}
    meta = {}
    for r in rows:
        k = (r.get("AttributeDescription") or "").strip()
        if not k:
            continue
        summary[k] = r.get("Value")
        unit[k] = (r.get("UnitDescription") or "").strip()
        meta = {
            "CommodityDescription": (r.get("CommodityDescription") or "").strip(),
            "CountryName": (r.get("CountryName") or "").strip(),
            "MarketYear": r.get("MarketYear"),
            "Month": r.get("Month"),
            "CalendarYear": r.get("CalendarYear"),
        }
    return summary, unit, meta


@app.route("/psd", methods=["GET"])
def psd():
    commodity_name = request.args.get("commodity", "")
    country_name = request.args.get("country", "mundo")
    year = request.args.get("year", "")

    if not commodity_name or not year:
        return jsonify({"error": "Use /psd?commodity=soja&country=brasil&year=2024"}), 400

    # 1) traduz commodity PT -> EN (se necessário)
    commodity_name2 = translate_commodity_name(commodity_name)

    # 2) resolve commodity code
    c_env, st = call_fas("LookupData/GetCommodities")
    if st != 200 or not c_env.get("ok"):
        return jsonify({"error": "Falha ao buscar commodities", "details": c_env}), 502

    commodities_list = c_env.get("data", [])
    commodity = find_best_match(commodities_list, commodity_name2, ["CommodityName", "commodityName", "Name", "name"])
    if not commodity:
        return jsonify({
            "error": f"Commodity não encontrada: {commodity_name}",
            "hint": "Tente também em inglês (soybeans, corn, rice) ou consulte /commodities."
        }), 404

    commodity_code = commodity.get("CommodityCode") or commodity.get("commodityCode") or commodity.get("Code")
    if not commodity_code:
        return jsonify({"error": "Não achei CommodityCode.", "sample": commodity}), 500

    # 3) baixar dados do ano (vem muitos países) e filtrar depois
    d_env, st = call_fas("CommodityData/GetCommodityDataByYear", params={"CommodityCode": commodity_code, "marketYear": int(year)})
    if st != 200 or not d_env.get("ok"):
        return jsonify({"error": "Falha ao buscar dados", "details": d_env}), 502

    rows = d_env.get("data", [])
    if not isinstance(rows, list) or not rows:
        return jsonify({"error": "Sem dados retornados para esse ano/commodity."}), 404

    # 4) filtrar país
    is_world = normalize(country_name) in ["world", "mundo", "global", "all"]
    if not is_world:
        p_env, st2 = call_fas("LookupData/GetCountries")
        if st2 != 200 or not p_env.get("ok"):
            return jsonify({"error": "Falha ao buscar países", "details": p_env}), 502

        countries_list = p_env.get("data", [])
        country = find_best_match(countries_list, country_name, ["CountryName", "countryName", "Name", "name"])
        if not country:
            return jsonify({"error": f"País não encontrado: {country_name}", "hint": "Consulte /countries."}), 404

        country_code = country.get("CountryCode") or country.get("countryCode") or country.get("Code")
        if not country_code:
            return jsonify({"error": "Não achei CountryCode.", "sample": country}), 500

        rows = [r for r in rows if (r.get("CountryCode") or "").strip() == country_code]
        if not rows:
            return jsonify({"error": f"Sem dados para o país: {country_name} nesse ano."}), 404

    # 5) filtrar para balanço + resumir
    rows2 = filter_to_balance_sheet(rows)
    summary, units, meta = summarize(rows2)

    return jsonify({
        "request": {"commodity": commodity_name, "country": country_name, "year": year},
        "resolved": {
            "CommodityCode": commodity_code,
            "CommodityName_found": (commodity.get("CommodityName") or "").strip(),
            "scope": "world" if is_world else "country"
        },
        "meta": meta,
        "balance_sheet": summary,
        "units": units
    }), 200
