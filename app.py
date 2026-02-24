import os
import re
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# Sua chave deve estar no Render em Environment Variables com o nome: FAS_API_KEY
FAS_KEY = os.getenv("FAS_API_KEY", "").strip()

# Base oficial do FAS Open Data (PSD)
BASE = "https://apps.fas.usda.gov/OpenData/api/psd"


def fas_get(path, params=None):
    """
    Faz uma chamada para a API do FAS (PSD) usando a chave do Render.
    """
    if not FAS_KEY:
        return {"error": "FAS_API_KEY não configurada no Render."}, 500

    headers = {"API_KEY": FAS_KEY}

    try:
        r = requests.get(f"{BASE}{path}", headers=headers, params=params, timeout=30)
    except Exception as e:
        return {"error": "Falha de conexão com a API do FAS.", "details": str(e)}, 502

    try:
        data = r.json()
    except Exception:
        data = {
            "error": "A resposta não veio em JSON.",
            "status_code": r.status_code,
            "text": (r.text or "")[:800]
        }

    return data, r.status_code


def normalize(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"\s+", " ", s)
    return s


def find_best_match(items, name_keys, wanted):
    """
    Procura um item na lista comparando por "contém" e depois por "igual".
    Isso permite você digitar "soja" ou "soy" e ele tentar achar.
    """
    w = normalize(wanted)
    if not w:
        return None

    # 1) tenta "contém"
    for it in items:
        for k in name_keys:
            v = it.get(k)
            if isinstance(v, str) and w in normalize(v):
                return it

    # 2) tenta "igual"
    for it in items:
        for k in name_keys:
            v = it.get(k)
            if isinstance(v, str) and w == normalize(v):
                return it

    return None


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "ok": True,
        "use": [
            "/_routes",
            "/health",
            "/commodities",
            "/countries",
            "/psd?commodity=soybeans&country=brazil&year=2024",
            "/psd?commodity=corn&country=world&year=2024",
            "/psd?commodity=rice&country=india&year=2024"
        ],
        "notes": [
            "Se /commodities e /countries retornarem Not Found, abra /_routes para ver as rotas que estão rodando.",
            "Se /commodities der 401/403, confira se a FAS_API_KEY está correta no Render."
        ]
    })


@app.route("/health", methods=["GET"])
def health():
    # Só para confirmar que o app está vivo e lendo a variável
    return jsonify({
        "ok": True,
        "fas_key_configured": bool(FAS_KEY),
        "base": BASE
    })


@app.route("/_routes", methods=["GET"])
def routes():
    # Lista as rotas ativas no servidor (ótimo pra diagnosticar)
    return jsonify(sorted([str(r) for r in app.url_map.iter_rules()]))


@app.route("/commodities", methods=["GET"])
def commodities():
    data, status = fas_get("/commodities")
    return jsonify(data), status


@app.route("/countries", methods=["GET"])
def countries():
    data, status = fas_get("/countries")
    return jsonify(data), status


@app.route("/psd", methods=["GET"])
def psd():
    """
    Endpoint "amigável":
    Você passa nomes (commodity, country, year) e ele resolve códigos sozinho.
    Exemplos:
      /psd?commodity=soybeans&country=brazil&year=2024
      /psd?commodity=corn&country=world&year=2024
    """
    commodity_name = request.args.get("commodity", "")
    country_name = request.args.get("country", "world")
    year = request.args.get("year", "")

    if not commodity_name or not year:
        return jsonify({
            "error": "Faltou parâmetro.",
            "use": "/psd?commodity=soybeans&country=brazil&year=2024"
        }), 400

    # 1) Busca lista de commodities e acha a escolhida
    commodities_list, st = fas_get("/commodities")
    if st != 200:
        return jsonify({"error": "Falha ao buscar commodities", "details": commodities_list}), st

    commodity = find_best_match(commodities_list, ["commodityName", "name", "description"], commodity_name)
    if not commodity:
        # retorna alguns exemplos para ajudar
        examples = [c.get("commodityName") for c in commodities_list[:10] if isinstance(c, dict)]
        return jsonify({
            "error": f"Commodity não encontrada: {commodity_name}",
            "hint": "Tente em inglês (ex.: soybeans, corn, rice) ou use /commodities para ver nomes.",
            "examples": examples
        }), 404

    commodity_code = commodity.get("commodityCode") or commodity.get("code") or commodity.get("id")
    if not commodity_code:
        return jsonify({"error": "Não achei o código da commodity no retorno da API.", "sample": commodity}), 500

    # 2) Se for mundo, usa endpoint world. Senão, resolve país.
    is_world = normalize(country_name) in ["world", "mundo", "global"]
    country_code = "World"

    if not is_world:
        countries_list, st = fas_get("/countries")
        if st != 200:
            return jsonify({"error": "Falha ao buscar países", "details": countries_list}), st

        country = find_best_match(countries_list, ["countryName", "name"], country_name)
        if not country:
            examples = [c.get("countryName") for c in countries_list[:10] if isinstance(c, dict)]
            return jsonify({
                "error": f"País não encontrado: {country_name}",
                "hint": "Tente em inglês (ex.: brazil, argentina, india) ou use /countries.",
                "examples": examples
            }), 404

        country_code = country.get("countryCode") or country.get("code") or country.get("id")
        if not country_code:
            return jsonify({"error": "Não achei o código do país no retorno da API.", "sample": country}), 500

    # 3) Busca os dados no FAS
    if is_world:
        data, st = fas_get(f"/commodity/{commodity_code}/world/year/{year}")
    else:
        data, st = fas_get(f"/commodity/{commodity_code}/country/{country_code}/year/{year}")

    return jsonify({
        "request": {"commodity": commodity_name, "country": country_name, "year": year},
        "resolved": {
            "commodityCode": commodity_code,
            "countryCode": country_code,
            "commodityName_found": commodity.get("commodityName") or commodity.get("name"),
        },
        "data": data
    }), st
