from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from pathlib import Path
import httpx
import base64
import re

app = FastAPI(title="FinAnalyza Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "cs,en;q=0.9",
}


@app.get("/", response_class=HTMLResponse)
async def root():
    """Serve the frontend app."""
    html_file = Path("index.html")
    if html_file.exists():
        return HTMLResponse(content=html_file.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>FinAnalyza Backend běží</h1><p>index.html nenalezen.</p>")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "FinAnalyza Backend"}


@app.get("/firma/{ico}")
async def get_firma(ico: str):
    """Vraci nazev firmy z ARES podle ICO."""
    ico = ico.zfill(8)
    url = f"https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty/{ico}"
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            res = await client.get(url)
            if res.status_code != 200:
                raise HTTPException(status_code=404, detail="ICO nenalezeno v ARES")
            data = res.json()
            nazev = data.get("obchodniJmeno") or data.get("nazev") or "Neznama firma"
            return {"ico": ico, "nazev": nazev}
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"ARES nedostupny: {e}")


@app.get("/dokumenty/{ico}")
async def get_dokumenty(ico: str, rok: int = None):
    """
    Vraci seznam dokumentu firmy z justice.cz.
    Pokud je zadan rok, filtruje prednostne ucetni zaverky za dany rok.
    Kazdy dokument ma: id, url, typ, rok (pokud se podarilo zjistit).
    """
    ico = ico.zfill(8)
    url = f"https://or.justice.cz/ias/ui/vypis-sl-firma?subjektId=&ico={ico}"

    async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=HEADERS) as client:
        try:
            res = await client.get(url)
            if res.status_code != 200:
                raise HTTPException(status_code=502, detail="Justice.cz nedostupna")

            html = res.text
            dokumenty = parse_dokumenty(html, rok)

            return {
                "ico": ico,
                "rok_filter": rok,
                "pocet": len(dokumenty),
                "dokumenty": dokumenty,
            }
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Justice.cz nedostupna: {e}")


def parse_dokumenty(html: str, rok_filter: int = None) -> list:
    """
    Parsuje HTML stranku justice.cz a vraci seznam dokumentu.
    Snazi se zjistit typ dokumentu a rok z kontextu radku.
    Radi: nejprve ucetni zaverky za pozadovany rok, pak ostatni.
    """
    dokumenty = []
    seen_ids = set()

    # justice.cz renders a table - each row has a download link + metadata
    # Try to find rows with document info
    # Pattern: find table rows containing download links
    row_pattern = re.compile(
        r'<tr[^>]*>(.*?)</tr>',
        re.DOTALL | re.IGNORECASE
    )
    
    for row_match in row_pattern.finditer(html):
        row = row_match.group(1)
        
        # Find download ID in this row
        id_match = re.search(r'/ias/content/download\?id=(\d+)', row)
        if not id_match:
            continue
        
        doc_id = id_match.group(1)
        if doc_id in seen_ids:
            continue
        seen_ids.add(doc_id)
        
        # Extract text content from row (strip HTML tags)
        row_text = re.sub(r'<[^>]+>', ' ', row)
        row_text = re.sub(r'\s+', ' ', row_text).strip()
        
        # Detect document type
        row_lower = row_text.lower()
        typ = "ostatni"
        priorita = 3
        
        if any(k in row_lower for k in ["účetní závěrka", "ucetni zaverka", "zavěrka", "zaverka"]):
            typ = "ucetni_zaverka"
            priorita = 1
        elif any(k in row_lower for k in ["výroční zpráva", "vyrocni zprava"]):
            typ = "vyrocni_zprava"
            priorita = 2
        
        # Try to extract year from row
        rok_dok = None
        year_matches = re.findall(r'\b(20\d{2})\b', row_text)
        if year_matches:
            # Take the most relevant year (prefer the one matching filter)
            if rok_filter and str(rok_filter) in year_matches:
                rok_dok = rok_filter
            else:
                rok_dok = int(year_matches[-1])
        
        # Boost priority if year matches
        if rok_filter and rok_dok == rok_filter:
            priorita -= 0.5
        
        dokumenty.append({
            "id": doc_id,
            "url": f"https://or.justice.cz/ias/content/download?id={doc_id}",
            "typ": typ,
            "rok": rok_dok,
            "priorita": priorita,
            "popis": row_text[:120],
        })
    
    # Fallback: if table parsing found nothing, just find all IDs
    if not dokumenty:
        for id_match in re.finditer(r'/ias/content/download\?id=(\d+)', html):
            doc_id = id_match.group(1)
            if doc_id not in seen_ids:
                seen_ids.add(doc_id)
                dokumenty.append({
                    "id": doc_id,
                    "url": f"https://or.justice.cz/ias/content/download?id={doc_id}",
                    "typ": "neznamy",
                    "rok": None,
                    "priorita": 2,
                    "popis": "",
                })
    
    # Sort: ucetni zaverky for requested year first, then by priority
    dokumenty.sort(key=lambda d: (d["priorita"], -(d["rok"] or 0)))
    
    return dokumenty


@app.get("/pdf/{doc_id}")
async def get_pdf(doc_id: str):
    """Stahne PDF z justice.cz a vrati ho jako base64."""
    if not doc_id.isdigit():
        raise HTTPException(status_code=400, detail="Neplatne ID dokumentu")

    url = f"https://or.justice.cz/ias/content/download?id={doc_id}"

    async with httpx.AsyncClient(timeout=60, follow_redirects=True, headers=HEADERS) as client:
        try:
            res = await client.get(url)
            if res.status_code != 200:
                raise HTTPException(status_code=404, detail=f"Dokument nenalezen (HTTP {res.status_code})")

            content_type = res.headers.get("content-type", "")
            # Accept PDF or unknown binary content
            if "html" in content_type:
                raise HTTPException(status_code=422, detail="Justice.cz vratilo HTML misto PDF - dokument neni dostupny")

            encoded = base64.b64encode(res.content).decode("utf-8")
            return {
                "doc_id": doc_id,
                "size_kb": round(len(res.content) / 1024),
                "content_type": content_type,
                "base64": encoded,
            }
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Chyba stazeni: {e}")
