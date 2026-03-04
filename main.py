from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
import base64
import re

app = FastAPI(title="FinAnalyza Backend")

# Allow requests from anywhere (the frontend runs in a browser)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


@app.get("/")
def root():
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
async def get_dokumenty(ico: str):
    """Vraci seznam PDF dokumentu firmy z justice.cz."""
    ico = ico.zfill(8)
    url = f"https://or.justice.cz/ias/ui/vypis-sl-firma?subjektId=&ico={ico}"

    async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=HEADERS) as client:
        try:
            res = await client.get(url)
            if res.status_code != 200:
                raise HTTPException(status_code=502, detail="Justice.cz nedostupna")

            html = res.text
            matches = re.findall(r"/ias/content/download\?id=(\d+)", html)
            # Deduplicate while preserving order
            seen = set()
            ids = [x for x in matches if not (x in seen or seen.add(x))]

            return {
                "ico": ico,
                "pocet": len(ids),
                "dokumenty": [
                    {"id": doc_id, "url": f"https://or.justice.cz/ias/content/download?id={doc_id}"}
                    for doc_id in ids
                ]
            }
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Justice.cz nedostupna: {e}")


@app.get("/pdf/{doc_id}")
async def get_pdf(doc_id: str):
    """Stahne PDF z justice.cz a vrati ho jako base64."""
    if not doc_id.isdigit():
        raise HTTPException(status_code=400, detail="Neplatne ID dokumentu")

    url = f"https://or.justice.cz/ias/content/download?id={doc_id}"

    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=HEADERS) as client:
        try:
            res = await client.get(url)
            if res.status_code != 200:
                raise HTTPException(status_code=404, detail="Dokument nenalezen")

            content_type = res.headers.get("content-type", "")
            if "pdf" not in content_type and len(res.content) < 1000:
                raise HTTPException(status_code=422, detail="Soubor neni PDF")

            encoded = base64.b64encode(res.content).decode("utf-8")
            return {
                "doc_id": doc_id,
                "size_kb": round(len(res.content) / 1024),
                "base64": encoded,
            }
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Chyba stazeni: {e}")
