import os
import re
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

FAS_KEY = os.getenv("FAS_API_KEY", "")
BASE = "https://apps.fas.usda.gov/OpenData/api/psd"

def fas_get(path, params=None):
    if not FAS_KEY:
        return {"error": "FAS_API_KEY não configurada no Render."}, 500
    headers = {"API_KEY": FAS_KEY}
    r = requests.get(f"{BASE}{path}", headers=headers, params=params, timeout=30)
    try:
        data = r.json()
    except Exception:
        data = {"error": "Resposta não-JSON", "status_code": r.status_code, "text": r.text[:500]}
    return data, r.status_code

def normalize(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"\s+", " ", s)
    return s

def find_best_match(items, name_keys, wanted):
    w = normalize(wanted)
    # 1) tenta "contém"
    for it in items:
        for k in name_keys:
            if k in it and isinstance(it[k], str):
                if w in normalize(it[k]):
                    return it
    # 2) tenta "igual"
    for it in items:
        for k in name_keys:
            if k in it and isinstance(it[k], str):
                if w == normalize(it[k]):
                    return it
    return None

@app.get("/")
def home():
    return jsonify({
        "ok": True,
        "use": [
            "/commodities",
            "/countries",
            "/psd?commodity=soja&country=brasil&year=2024",
            "/psd?commodity=milho&country=world&year=2024"
        ]
    })

@app.get("/commodities")
def commodities():
    data, status = fas_get("/commodities")
    return jsonify(data), status

@app.get("/countries")
def countries():
    data, status = fas_get("/countries")
    return jsonify(data), status

@app.get("/psd")
def psd():
    commodity_name = request.args.get("commodity", "")
    country_name = request.args.get("country", "world")
    year = request.args.get("year", "")

    if not commodity_name or not year:
        return jsonify({"error": "Use /psd?commodity=soja&country=brasil&year=2024"}), 400

    # (1) descobre o código da commodity sozinho
    commodities, st = fas_get("/commodities")
    if st != 200:
        return jsonify({"error": "Falha ao buscar commodities", "details": commodities}), st

    commodity = find_best_match(commodities, ["commodityName", "name", "description"], commodity_name)
    if not commodity:
        return jsonify({"error": f"Commodity não encontrada: {commodity_name}"}), 404

    commodity_code = commodity.get("commodityCode") or commodity.get("code") or commodity.get("id")
    if not commodity_code:
        return jsonify({"error": "Não achei o código da commodity no retorno da API.", "sample": commodity}), 500

    # (2) descobre o código do país sozinho (ou usa "World")
    country_code = "World"
    is_world = normalize(country_name) in ["world", "mundo", "global"]

    if not is_world:
        countries, st = fas_get("/countries")
        if st != 200:
            return jsonify({"error": "Falha ao buscar países", "details": countries}), st

        country = find_best_match(countries, ["countryName", "name"], country_name)
        if not country:
            return jsonify({"error": f"País não encontrado: {country_name}"}), 404

        country_code = country.get("countryCode") or country.get("code") or country.get("id")
        if not country_code:
            return jsonify({"error": "Não achei o código do país no retorno da API.", "sample": country}), 500

    # (3) busca os dados
    if is_world:
        data, st = fas_get(f"/commodity/{commodity_code}/world/year/{year}")
    else:
        data, st = fas_get(f"/commodity/{commodity_code}/country/{country_code}/year/{year}")

    return jsonify({
        "request": {"commodity": commodity_name, "country": country_name, "year": year},
        "resolved": {"commodityCode": commodity_code, "countryCode": country_code},
        "data": data
    }), st