# main.py
import os
import shutil
import urllib.parse
from typing import Optional, Tuple, List, Dict, Any
import re

import aiofiles
from fastapi import FastAPI, Request, Response, HTTPException, Body, Query
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from jinja2 import Environment, FileSystemLoader
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
import httpx

app = FastAPI(title="BTU Courses - FastAPI Playwright Scraper")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # or your frontend URL
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- Configuration ----------------
BASE_URL = "https://classroom.btu.edu.ge/en/student/me/courses"
HTML_DIR = "html"
COURSES_DIR = "courses"
TEMPLATES_DIR = "templates"
COOKIE = os.getenv("BTU_COOKIE", None)

os.makedirs(HTML_DIR, exist_ok=True)
os.makedirs(COURSES_DIR, exist_ok=True)
os.makedirs(TEMPLATES_DIR, exist_ok=True)

jinja_env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=True)

# ---------------- Playwright fetch ----------------
async def fetch_text_playwright(url: str, cookie: Optional[str] = None) -> str:
    """Fetch JS-rendered page using Playwright."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        if cookie:
            cookies_list = []
            for c in cookie.split(";"):
                if "=" in c:
                    name, value = c.strip().split("=", 1)
                    cookies_list.append({"name": name, "value": value, "domain": ".btu.edu.ge", "path": "/"})
            if cookies_list:
                await context.add_cookies(cookies_list)
        page = await context.new_page()
        await page.goto(url, timeout=60000)

        # --- Wait for table to load to avoid 0 courses ---
        try:
            await page.wait_for_selector("table.table.table-striped.table-bordered.table-hover.fluid", timeout=15000)
        except Exception:
            pass  # fallback if selector not found

        await page.wait_for_timeout(1000)  # extra 1s to ensure JS finishes
        html = await page.content()
        await browser.close()
        return html

# ---------------- HTTPX fetch ----------------
async def fetch_bytes(url: str, cookie: Optional[str] = None, client: Optional[httpx.AsyncClient] = None) -> bytes:
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "*/*", "Cookie": cookie or COOKIE}
    close_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=60.0, follow_redirects=True)
        close_client = True
    try:
        r = await client.get(url, headers=headers)
        r.raise_for_status()
        return r.content
    finally:
        if close_client:
            await client.aclose()

# ---------------- Parsing helpers ----------------
def parse_num(td_text: str):
    if td_text is None:
        return None
    txt = td_text.strip().replace(",", ".")
    try:
        return float(txt)
    except Exception:
        return txt.strip()

def parse_courses(html: str) -> Tuple[List[Dict[str, Any]], Optional[float]]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.table.table-striped.table-bordered.table-hover.fluid")
    if not table:
        return [], None
    tbody = table.find("tbody")
    if not tbody:
        return [], None

    courses = []
    total_ects = None

    for tr in tbody.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) == 2 and not tds[0].get_text(strip=True):
            total_ects = parse_num(tds[-1].get_text(strip=True))
            continue
        if len(tds) != 6:
            continue
        name_a = tds[2].find("a")
        name = name_a.get_text(strip=True) if name_a else tds[2].get_text(strip=True)
        grade = parse_num(tds[3].get_text(strip=True))
        ects = parse_num(tds[5].get_text(strip=True))
        url = name_a["href"] if name_a and name_a.has_attr("href") else None
        if url and not urllib.parse.urlparse(url).netloc:
            url = urllib.parse.urljoin(BASE_URL, url)
        courses.append({"name": name, "grade": grade, "ects": ects, "url": url})

    return courses, total_ects

# ---------------- File saving ----------------
async def save_bytes(path: str, data: bytes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    async with aiofiles.open(path, "wb") as f:
        await f.write(data)

# ---------------- Course fetch ----------------
async def fetch_course_pages(course: Dict[str, Any], cookie: Optional[str] = None) -> Dict[str, Any]:
    if not course.get("url"):
        return {}
    course_name = course["name"]
    safe_name = "".join(c for c in course_name if c.isalnum() or c in (" ", "-", "_")).strip()
    html_folder = os.path.join(HTML_DIR, safe_name)
    course_folder = os.path.join(COURSES_DIR, safe_name)
    os.makedirs(html_folder, exist_ok=True)
    os.makedirs(course_folder, exist_ok=True)

    course_html = await fetch_text_playwright(course["url"], cookie=cookie)
    async with aiofiles.open(os.path.join(html_folder, "course.html"), "w", encoding="utf-8") as f:
        await f.write(course_html)

    return {"course_html_path": os.path.join(html_folder, "course.html"), "html_folder": html_folder, "course_folder": course_folder}

# ---------------- API routes ----------------
@app.get("/api/courses")
async def api_courses():
    if not COOKIE:
        raise HTTPException(status_code=400, detail="Missing BTU_COOKIE environment variable")
    html = await fetch_text_playwright(BASE_URL, cookie=COOKIE)
    courses, total_ects = parse_courses(html)
    return {"courses": courses, "total_ects": total_ects}

@app.post("/api/fetch")
async def api_fetch(url: str = Body(...), binary: bool = Body(False)):
    try:
        if binary:
            b = await fetch_bytes(url, cookie=COOKIE)
            return Response(content=b, media_type="application/octet-stream")
        else:
            txt = await fetch_text_playwright(url, cookie=COOKIE)
            return HTMLResponse(txt)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/api/proxy")
async def api_proxy(url: str = Query(...)):
    if not url:
        raise HTTPException(status_code=400, detail="Missing url")
    try:
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "*/*", "Cookie": COOKIE} if COOKIE else {"User-Agent": "Mozilla/5.0"}
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            r = await client.get(url, headers=headers, stream=True)
            r.raise_for_status()
            async def streamer():
                async for chunk in r.aiter_bytes():
                    yield chunk
            content_type = r.headers.get("content-type", "application/octet-stream")
            return StreamingResponse(streamer(), media_type=content_type)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

# ---------------- Static files ----------------
app.mount("/html", StaticFiles(directory=HTML_DIR), name="html")
app.mount("/courses", StaticFiles(directory=COURSES_DIR), name="courses")

@app.get("/")
async def index():
    courses_list = sorted(os.listdir(COURSES_DIR))
    html_list = "<ul>"
    for c in courses_list:
        html_list += f'<li><a href="/courses/{c}/course.html">{c}</a></li>'
    html_list += "</ul>"
    return HTMLResponse(f"<h1>Courses</h1>{html_list}")
