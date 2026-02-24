import os
import re
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# Sua chave deve estar no Render em Environment Variables com o nome: FAS_API_KEY
FAS_KEY = (os.getenv("FAS_API_KEY", "") or "").strip()

# Base oficial do FAS Open Data (PSD)
BASE = "https://apps.fas.usda.gov/OpenData/api/psd"


def fas_get(path, params=None):
    """
    Chama a API do FAS e devolve também status_code e um resumo útil.
    IMPORTANTe: envia a chave de 2 formas (header e parâmetro),
    porque o gateway às vezes exige um dos dois.
    """
    if not FAS_KEY:
        return {
            "ok": False,
            "status_code": 500,
            "error": "FAS_API_KEY não configurada no Render."
        }, 500

    params = dict(params or {})
    # Alguns gateways aceitam por query param como api_key
    params.setdefault("api_key", FAS_KEY)

    headers = {
        # Forma que o Swagger costuma usar:
        "API_KEY": FAS_KEY,
        # Forma alternativa comum:
        "X-Api-Key": FAS_KEY,
        # Evita bloqueio bobo por falta de user-agent
        "User-Agent": "fas-psd-render/1.0 (+https://render.com)"
    }

    url = f"{BASE}{path}"

    try:
        r = requests.get(url, headers=headers, params=params, timeout=30)
    except Exception as e:
        return {
            "ok": False,
            "status_code": 502,
            "error": "Falha de conexão com a API do FAS.",
            "details": str(e),
            "url": url
        }, 502

    # Tenta JSON; se não vier JSON, guarda um pedaço do texto
    try:
        data = r.json()
    except Exception:
        data = {"raw_text": (r.text or "")[:1200]}

    # Sempre devolvemos um envelope com status + url
    envelope = {
        "ok": (200 <= r.status_code < 300),
        "status_code": r.status_code,
        "url": r.url,
        "data": data
    }
    return envelope, r.status_code


def normalize(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"\s+", " ", s)
    return s


def find_best_match(items, name_keys, wanted):
    w = normalize(wanted)
    if not w:
        return None

    # 1) tenta "contém"
    for it in items:
        if not isinstance(it, dict):
            continue
        for k in name_keys:
            v = it.get(k)
            if isinstance(v, str) and w in normalize(v):
                return it

    # 2) tenta "igual"
    for it in items:
        if not isinstance(it, dict):
            continue
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
            "/health",
            "/_routes",
            "/diagnose_fas",
            "/commodities",
            "/countries",
            "/psd?commodity=soybeans&country=brazil&year=2024",
            "/psd?commodity=corn&country=world&year=2024"
        ]
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


@app.route("/diagnose_fas", methods=["GET"])
def diagnose_fas():
    """
    Diagnóstico rápido: mostra o status real que a USDA está devolvendo
    para endpoints simples (commodities/countries).
    """
    a, _ = fas_get("/commodities")
    b, _ = fas_get("/countries")
    return jsonify({
        "commodities_probe": a,
        "countries_probe": b
    })


@app.route("/commodities", methods=["GET"])
def commodities():
    envelope, status = fas_get("/commodities")
    return jsonify(envelope), status


@app.route("/countries", methods=["GET"])
def countries():
    envelope, status = fas_get("/countries")
    return jsonify(envelope), status


@app.route("/psd", methods=["GET"])
def psd():
    commodity_name = request.args.get("commodity", "")
    country_name = request.args.get("country", "world")
    year = request.args.get("year", "")

    if not commodity_name or not year:
        return jsonify({
            "error": "Use assim:",
            "example": "/psd?commodity=soybeans&country=brazil&year=2024"
        }), 400

    # 1) lista commodities
    commodities_env, st = fas_get("/commodities")
    if st != 200 or not commodities_env.get("ok"):
        return jsonify({"error": "Falha ao buscar commodities", "details": commodities_env}), 502

    commodities_list = commodities_env.get("data", [])
    commodity = find_best_match(commodities_list, ["commodityName", "name", "description"], commodity_name)
    if not commodity:
        return jsonify({
            "error": f"Commodity não encontrada: {commodity_name}",
            "hint": "Teste em inglês primeiro: soybeans, corn, rice. Depois ajustamos para PT automaticamente."
        }), 404

    commodity_code = commodity.get("commodityCode") or commodity.get("code") or commodity.get("id")
    if not commodity_code:
        return jsonify({"error": "Não achei commodityCode no retorno.", "sample": commodity}), 500

    # 2) mundo vs país
    is_world = normalize(country_name) in ["world", "mundo", "global"]
    if is_world:
        data_env, st = fas_get(f"/commodity/{commodity_code}/world/year/{year}")
        return jsonify({
            "request": {"commodity": commodity_name, "country": country_name, "year": year},
            "resolved": {"commodityCode": commodity_code, "country": "World"},
            "result": data_env
        }), st

    countries_env, st = fas_get("/countries")
    if st != 200 or not countries_env.get("ok"):
        return jsonify({"error": "Falha ao buscar países", "details": countries_env}), 502

    countries_list = countries_env.get("data", [])
    country = find_best_match(countries_list, ["countryName", "name", "countryDescription"], country_name)
    if not country:
        return jsonify({"error": f"País não encontrado: {country_name}", "hint": "Teste em inglês: brazil, argentina, india."}), 404

    country_code = country.get("countryCode") or country.get("code") or country.get("id")
    if not country_code:
        return jsonify({"error": "Não achei countryCode no retorno.", "sample": country}), 500

    data_env, st = fas_get(f"/commodity/{commodity_code}/country/{country_code}/year/{year}")
    return jsonify({
        "request": {"commodity": commodity_name, "country": country_name, "year": year},
        "resolved": {"commodityCode": commodity_code, "countryCode": country_code},
        "result": data_env
    }), st
