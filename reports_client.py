import io
import re
import requests
from pypdf import PdfReader


REPORTS = {
    "grain": {
        "name": "Grain: World Markets and Trade",
        "url": "https://apps.fas.usda.gov/psdonline/circulars/grain.pdf",
        "commodities": ["trigo", "wheat", "milho", "corn", "arroz", "rice"]
    },
    "oilseeds": {
        "name": "Oilseeds: World Markets and Trade",
        "url": "https://apps.fas.usda.gov/psdonline/circulars/oilseeds.pdf",
        "commodities": ["soja", "soybean", "soybeans"]
    },
    "coffee": {
        "name": "Coffee: World Markets and Trade",
        "url": "https://apps.fas.usda.gov/psdonline/circulars/coffee.pdf",
        "commodities": ["cafe", "café", "coffee"]
    },
    "production": {
        "name": "World Agricultural Production",
        "url": "https://apps.fas.usda.gov/psdonline/circulars/production.pdf",
        "commodities": ["safra", "produção", "production", "clima", "weather"]
    }
}


_REPORT_CACHE = {}


def normalize_text(text):
    text = text or ""
    text = text.replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def download_pdf(url):
    response = None
    last_error = None

    for attempt in range(1, 4):
        try:
            response = requests.get(
                url,
                timeout=(45, 180),
                headers={"User-Agent": "fas-psd-render-reports/1.1"}
            )
            response.raise_for_status()
            return response

        except Exception as e:
            last_error = str(e)
            response = None

    return {
        "status": "error",
        "message": "Falha ao baixar o relatório PDF após 3 tentativas.",
        "details": last_error,
        "source_url": url
    }


def fetch_pdf_text(report_id):
    report_id = (report_id or "").strip().lower()

    if report_id not in REPORTS:
        return {
            "status": "error",
            "message": f"Relatório não encontrado: {report_id}",
            "available_reports": list(REPORTS.keys())
        }

    if report_id in _REPORT_CACHE:
        return _REPORT_CACHE[report_id]

    report = REPORTS[report_id]
    url = report["url"]

    response = download_pdf(url)

    if isinstance(response, dict) and response.get("status") == "error":
        return response

    try:
        reader = PdfReader(io.BytesIO(response.content))
        pages = []

        for i, page in enumerate(reader.pages):
            page_text = page.extract_text() or ""
            page_text = normalize_text(page_text)

            if page_text:
                pages.append({
                    "page": i + 1,
                    "text": page_text
                })

    except Exception as e:
        return {
            "status": "error",
            "message": "Falha ao extrair texto do PDF.",
            "details": str(e),
            "source_url": url
        }

    data = {
        "status": "ok",
        "report_id": report_id,
        "report_name": report["name"],
        "source_url": url,
        "pages": pages,
        "page_count": len(pages)
    }

    _REPORT_CACHE[report_id] = data
    return data


def infer_report_from_commodity(commodity):
    q = (commodity or "").strip().lower()

    if q in ["soja", "soybean", "soybeans"]:
        return "oilseeds"

    if q in ["milho", "corn", "trigo", "wheat", "arroz", "rice"]:
        return "grain"

    if q in ["cafe", "café", "coffee"]:
        return "coffee"

    return "grain"


def split_into_snippets(text):
    parts = re.split(r"\n\s*\n|(?<=[.!?])\s+(?=[A-Z])", text)
    clean = []
    buffer = ""

    for part in parts:
        part = part.strip()

        if not part:
            continue

        if len(buffer) + len(part) < 1200:
            buffer = (buffer + " " + part).strip()
        else:
            if buffer:
                clean.append(buffer)
            buffer = part

    if buffer:
        clean.append(buffer)

    return clean


def search_report(report_id=None, query="", commodity=None, max_results=8):
    query = (query or "").strip()

    if not report_id:
        report_id = infer_report_from_commodity(commodity or query)

    report_id = (report_id or "").strip().lower()

    data = fetch_pdf_text(report_id)

    if data.get("status") != "ok":
        return data

    terms = [
        t.lower()
        for t in re.split(r"\s+", query)
        if len(t.strip()) >= 3
    ]

    if commodity:
        terms.extend([
            t.lower()
            for t in re.split(r"\s+", commodity)
            if len(t.strip()) >= 3
        ])

    terms = list(dict.fromkeys(terms))

    matches = []

    for page in data["pages"]:
        text = page["text"]
        text_lower = text.lower()

        page_score = sum(1 for term in terms if term in text_lower)

        if page_score <= 0:
            continue

        snippets = split_into_snippets(text)

        for snippet in snippets:
            snippet_lower = snippet.lower()
            snippet_score = sum(1 for term in terms if term in snippet_lower)

            if snippet_score > 0:
                matches.append({
                    "page": page["page"],
                    "score": snippet_score,
                    "text": snippet[:2500]
                })

    matches.sort(key=lambda x: x["score"], reverse=True)

    return {
        "status": "ok",
        "report_id": report_id,
        "report_name": data["report_name"],
        "source_url": data["source_url"],
        "query": query,
        "commodity": commodity,
        "matches": matches[:max_results],
        "total_matches_found": len(matches)
    }
