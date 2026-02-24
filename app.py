import os
import re
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

FAS_KEY = (os.getenv("FAS_API_KEY", "") or "").strip()

# PSDOnlineDataServices (o que aparece na sua tela do Swagger)
BASE = "https://apps.fas.usda.gov/PSDOnlineDataServices/api"


def call_fas(endpoint, params=None):
    """
    Chama o PSDOnlineDataServices enviando a chave como header API_KEY.
    """
    if not FAS_KEY:
        return {"ok": False, "error": "FAS_API_KEY não configurada no Render."}, 500

    url = f"{BASE}/{endpoint.lstrip('/')}"
    headers = {
        "API_KEY": FAS_KEY,
        "User-Agent": "fas-psd-render/1.0 (+https://render.com)"
    }

    try:
        r = requests.get(url, headers=headers, params=params or {}, timeout=30)
    except Exception as e:
        return {"ok": False, "error": "Falha de conexão com a API do FAS.", "details": str(e), "url": url}, 502

    # Tenta JSON; se não vier JSON, guarda texto
    try:
        data = r.json()
    except Exception:
        data = {"raw_text": (r.text or "")[:2000]}

    return {
        "ok": 200 <= r.status_code < 300,
        "status_code": r.status_code,
        "url": r.url,
        "data": data
    }, r.status_code


def normalize(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"\s+", " ", s)
    return s


def find_best_match(items, wanted, keys):
    """
    Procura um item por nome (contém / igual).
    """
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
            "/diagnose",
            "/commodities",
            "/countries",
            "/psd?commodity=soybeans&country=brazil&year=2024",
            "/psd?commodity=corn&country=world&year=2024"
        ],
        "note": "Agora estamos usando PSDOnlineDataServices (não OpenData)."
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "fas_key_configured": bool(FAS_KEY),
        "base": BASE
    })


@app.route("/_routes", methods=["GET"])
def routes():
    return jsonify(sorted([str(r) for r in app.url_map.iter_rules()]))


@app.route("/diagnose", methods=["GET"])
def diagnose():
    """
    Testa os endpoints mais comuns dessa API para confirmar acesso.
    """
    tests = {
        "commodities": "LookupData/GetCommodities",
        "countries": "LookupData/GetCountries",
        "sample_data": "CommodityData/GetCommodityDataByYear"
    }

    results = {}
    # probes simples
    env, st = call_fas(tests["commodities"])
    results["LookupData/GetCommodities"] = env

    env, st = call_fas(tests["countries"])
    results["LookupData/GetCountries"] = env

    # probe de dados (precisa de parâmetros)
    env, st = call_fas(tests["sample_data"], params={"CommodityCode": "0440000", "marketYear": 2024})
    results["CommodityData/GetCommodityDataByYear (corn, 2024)"] = env

    return jsonify(results)


@app.route("/commodities", methods=["GET"])
def commodities():
    env, st = call_fas("LookupData/GetCommodities")
    return jsonify(env), st


@app.route("/countries", methods=["GET"])
def countries():
    env, st = call_fas("LookupData/GetCountries")
    return jsonify(env), st


@app.route("/psd", methods=["GET"])
def psd():
    """
    Você chama assim:
      /psd?commodity=soja&country=brasil&year=2024

    Por enquanto, para garantir que funcione sem “mágica”,
    teste com commodity em inglês primeiro (soybeans/corn/rice).
    Depois eu te deixo PT automático.
    """
    commodity_name = request.args.get("commodity", "")
    country_name = request.args.get("country", "world")
    year = request.args.get("year", "")

    if not commodity_name or not year:
        return jsonify({"error": "Use /psd?commodity=soybeans&country=brazil&year=2024"}), 400

    # 1) pega lista de commodities (LookupData)
    c_env, st = call_fas("LookupData/GetCommodities")
    if st != 200 or not c_env.get("ok"):
        return jsonify({"error": "Falha ao buscar commodities", "details": c_env}), 502

    commodities_list = c_env.get("data", [])
    # Normalmente os campos são CommodityName / CommodityCode
    commodity = find_best_match(commodities_list, commodity_name, keys=["CommodityName", "commodityName", "Name", "name"])
    if not commodity:
        return jsonify({
            "error": f"Commodity não encontrada: {commodity_name}",
            "hint": "Teste em inglês: soybeans, corn, rice. (Depois eu coloco PT automático.)"
        }), 404

    commodity_code = commodity.get("CommodityCode") or commodity.get("commodityCode") or commodity.get("Code")
    if not commodity_code:
        return jsonify({"error": "Não achei CommodityCode no retorno.", "sample": commodity}), 500

    # 2) país: world ou resolve via LookupData/GetCountries
    is_world = normalize(country_name) in ["world", "mundo", "global"]
    country_code = None

    if not is_world:
        p_env, st = call_fas("LookupData/GetCountries")
        if st != 200 or not p_env.get("ok"):
            return jsonify({"error": "Falha ao buscar países", "details": p_env}), 502

        countries_list = p_env.get("data", [])
        # Normalmente: CountryName / CountryCode (2 letras)
        country = find_best_match(countries_list, country_name, keys=["CountryName", "countryName", "Name", "name"])
        if not country:
            return jsonify({
                "error": f"País não encontrado: {country_name}",
                "hint": "Teste em inglês: brazil, argentina, india."
            }), 404

        country_code = country.get("CountryCode") or country.get("countryCode") or country.get("Code")
        if not country_code:
            return jsonify({"error": "Não achei CountryCode no retorno.", "sample": country}), 500

    # 3) busca dados
    params = {"CommodityCode": commodity_code, "marketYear": int(year)}
    if not is_world and country_code:
        params["country"] = country_code  # filtro nem sempre funciona perfeitamente; mas tentamos

    d_env, st = call_fas("CommodityData/GetCommodityDataByYear", params=params)

    return jsonify({
        "request": {"commodity": commodity_name, "country": country_name, "year": year},
        "resolved": {
            "CommodityCode": commodity_code,
            "CountryCode": (country_code if country_code else "World/All")
        },
        "result": d_env
    }), st
