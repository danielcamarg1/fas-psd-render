import re
import requests
from bs4 import BeautifulSoup


WASDE_PUBLICATION_URL = "https://esmis.nal.usda.gov/publication/world-agricultural-supply-and-demand-estimates"


def clean_text(text):
    text = text or ""
    text = text.replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def absolute_url(href):
    if not href:
        return None

    if href.startswith("http://") or href.startswith("https://"):
        return href

    if href.startswith("/"):
        return "https://esmis.nal.usda.gov" + href

    return "https://esmis.nal.usda.gov/" + href.lstrip("./")


def get_latest_wasde_links():
    response = requests.get(
        WASDE_PUBLICATION_URL,
        timeout=30,
        headers={"User-Agent": "fas-psd-render-wasde/1.0"}
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    links = []

    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        text = a.get_text(" ", strip=True)
        href_lower = href.lower()

        if any(ext in href_lower for ext in [".txt", ".xml", ".pdf"]):
            links.append({
                "label": text,
                "url": absolute_url(href)
            })

    return links


def choose_best_wasde_link(links):
    priorities = [".txt", ".xml", ".pdf"]

    for ext in priorities:
        for link in links:
            url = (link.get("url") or "").lower()
            if ext in url:
                return link

    return None


def fetch_latest_wasde_text():
    links = get_latest_wasde_links()
    best_link = choose_best_wasde_link(links)

    if not best_link:
        return {
            "status": "error",
            "message": "Nenhum link TXT, XML ou PDF encontrado na página oficial do WASDE.",
            "links_found": links[:20]
        }

    url = best_link["url"]

    response = requests.get(
        url,
        timeout=60,
        headers={"User-Agent": "fas-psd-render-wasde/1.0"}
    )
    response.raise_for_status()

    content_type = response.headers.get("content-type", "").lower()
    url_lower = url.lower()

    if ".txt" in url_lower or "text/plain" in content_type:
        text = response.text
        format_used = "txt"

    elif ".xml" in url_lower or "xml" in content_type:
        text = BeautifulSoup(response.text, "xml").get_text("\n")
        format_used = "xml"

    else:
        return {
            "status": "error",
            "message": "O link encontrado não é TXT/XML. A leitura de PDF será adicionada em etapa posterior.",
            "source_url": url,
            "content_type": content_type
        }

    text = clean_text(text)

    return {
        "status": "ok",
        "report": "WASDE",
        "format_used": format_used,
        "source_url": url,
        "text": text,
        "text_length": len(text)
    }


def search_wasde(query, max_results=10):
    query = (query or "").strip()

    if not query:
        return {
            "status": "error",
            "message": "Informe uma consulta em query."
        }

    data = fetch_latest_wasde_text()

    if data.get("status") != "ok":
        return data

    text = data.get("text", "")
    paragraphs = re.split(r"\n\s*\n", text)

    query_terms = [
        term.lower()
        for term in re.split(r"\s+", query)
        if len(term.strip()) >= 3
    ]

    matches = []

    for paragraph in paragraphs:
        paragraph_clean = paragraph.strip()
        paragraph_lower = paragraph_clean.lower()

        if not paragraph_clean:
            continue

        score = sum(1 for term in query_terms if term in paragraph_lower)

        if score > 0:
            matches.append({
                "score": score,
                "text": paragraph_clean[:3000]
            })

    matches = sorted(matches, key=lambda x: x["score"], reverse=True)

    return {
        "status": "ok",
        "report": "WASDE",
        "format_used": data.get("format_used"),
        "source_url": data.get("source_url"),
        "query": query,
        "matches": matches[:max_results],
        "total_matches_found": len(matches)
    }
