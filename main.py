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
from playwright.async_api import async_playwright

app = FastAPI(title="BTU Courses - FastAPI proxy & scraper")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # or your frontend URL
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration
BASE_URL = "https://classroom.btu.edu.ge/en/student/me/courses"
TEMPLATES_DIR = "templates"
TEMPLATE_NAME = "template.html"
HTML_DIR = "html"
COURSES_DIR = "courses"
INDEX_HTML = "index.html"

COOKIE = os.getenv("BTU_COOKIE", None)

# Ensure folders exist
os.makedirs(HTML_DIR, exist_ok=True)
os.makedirs(COURSES_DIR, exist_ok=True)
os.makedirs(TEMPLATES_DIR, exist_ok=True)

# Jinja2 environment
jinja_env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=True)

# ------------------ Playwright fetch ------------------
async def fetch_text_playwright(url: str, cookie: Optional[str] = None) -> str:
    """Fetch page HTML using Playwright to bypass Cloudflare/JS."""
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
        content = await page.content()
        await browser.close()
        return content

# ------------------ HTTPX fetch for binaries ------------------
import httpx

async def fetch_bytes(url: str, cookie: Optional[str] = None, client: Optional[httpx.AsyncClient] = None) -> bytes:
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "*/*", "Cookie": cookie or COOKIE}
    close_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=60.0, follow_redirects=True)
        close_client = True
    try:
        r = await client.get(url, headers=headers)
        if r.status_code == 403:
            raise HTTPException(
                status_code=403,
                detail=f"Access forbidden: 403. Invalid/expired cookie or Cloudflare blocked request. URL: {url}"
            )
        r.raise_for_status()
        return r.content
    finally:
        if close_client:
            await client.aclose()

# ------------------ Parsing helpers ------------------
def parse_num(td_text: str):
    if td_text is None:
        return None
    txt = td_text.strip().replace(",", ".")
    try:
        return float(txt)
    except Exception:
        return txt.strip()

from bs4 import BeautifulSoup

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

def extract_course_urls(html: str) -> Dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    urls = {}
    tabs = soup.select_one("#course_tabs")
    if tabs:
        for link in tabs.find_all("a", href=True):
            href = link["href"]
            if "silabus" in href:
                urls["syllabus"] = urllib.parse.urljoin(BASE_URL, href)
            elif "groups" in href:
                urls["groups"] = urllib.parse.urljoin(BASE_URL, href)
            elif "scores" in href:
                urls["scores"] = urllib.parse.urljoin(BASE_URL, href)
            elif "files" in href:
                urls["files"] = urllib.parse.urljoin(BASE_URL, href)
    syllabus_file = soup.select_one('a[href*="courseSilabusFile"]')
    if syllabus_file:
        href = syllabus_file["href"]
        urls["syllabus_file"] = urllib.parse.urljoin(BASE_URL, href)
    return urls

def parse_scores(html: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    data = {"group": None, "lector": None, "assessments": []}
    h4 = soup.select_one(".tab_scores h4")
    if h4:
        text = h4.get_text(" ", strip=True)
        if "Group" in text:
            parts = text.split(" - ", 1)
            data["group"] = parts[0].replace("Group", "").strip()
        lector_link = h4.select_one("a[href*='/lector/']")
        if lector_link:
            data["lector"] = lector_link.get_text(strip=True)
    table = soup.select_one(".tab_scores table")
    if table:
        for tr in table.select("tbody tr"):
            tds = tr.find_all("td")
            if len(tds) != 2:
                continue
            component = tds[0].get_text(strip=True)
            score = tds[1].get_text(strip=True)
            if component in ("სულ", "Credits") or "გამოცდაზე გასვლის" in component:
                continue
            if component:
                max_points = None
                max_match = re.search(r'max\.?\s*([\d.,]+)', component)
                if max_match:
                    try:
                        max_points = float(max_match.group(1).replace(",", "."))
                    except ValueError:
                        pass
                data["assessments"].append({"component": component, "score": score or None, "max_points": max_points})
    return data

def parse_files(html: str, my_lector: Optional[str] = None) -> List[Dict[str, Optional[str]]]:
    soup = BeautifulSoup(html, "html.parser")
    materials = []
    current_lector = None
    table = soup.select_one("#files")
    if not table:
        return materials
    for tr in table.find_all("tr"):
        lector_link = tr.select_one("a[href*='/lector/']")
        tr_class = tr.get("class") or []
        if lector_link and "info" in tr_class:
            current_lector = lector_link.get_text(strip=True)
            continue
        if my_lector and current_lector and current_lector.lower() != my_lector.lower():
            continue
        tds = tr.find_all("td")
        if not tds:
            continue
        file_link = tds[0].select_one("a[href*='/uploads/']")
        name = tds[0].get_text(strip=True)
        url = file_link["href"] if file_link and file_link.get("href") else None
        if url:
            url = urllib.parse.urljoin(BASE_URL, url)
        ext_link = tds[1].select_one("a") if len(tds) > 1 else None
        ext_url = ext_link["href"] if ext_link else None
        if ext_url:
            ext_url = urllib.parse.urljoin(BASE_URL, ext_url)
        if name:
            materials.append({"name": name, "url": url, "external_url": ext_url})
    return materials

def parse_groups(html: str) -> Dict[str, List[str]]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("#groups")
    if not table:
        return {"groups": []}
    groups = []
    for tr in table.find_all("tr"):
        if "warning" in (tr.get("class") or []):
            continue
        text = tr.get_text(strip=True)
        if text and "Not found" not in text:
            groups.append(text)
    return {"groups": groups}

# ------------------ File saving ------------------
async def save_bytes(path: str, data: bytes):
    dirpath = os.path.dirname(path)
    if dirpath and not os.path.exists(dirpath):
        os.makedirs(dirpath, exist_ok=True)
    async with aiofiles.open(path, "wb") as f:
        await f.write(data)

# ------------------ Course fetch ------------------
async def fetch_course_pages(course: Dict[str, Any], cookie: Optional[str] = None) -> Dict[str, Any]:
    """Fetch course page & subpages using Playwright."""
    if not course.get("url"):
        return {}
    course_name = course["name"]
    safe_name = "".join(c for c in course_name if c.isalnum() or c in (" ", "-", "_")).strip()
    html_folder = os.path.join(HTML_DIR, safe_name)
    course_folder = os.path.join(COURSES_DIR, safe_name)
    os.makedirs(html_folder, exist_ok=True)
    os.makedirs(course_folder, exist_ok=True)
    os.makedirs(os.path.join(course_folder, "material"), exist_ok=True)

    course_html = await fetch_text_playwright(course["url"], cookie=cookie)
    async with aiofiles.open(os.path.join(html_folder, "course.html"), "w", encoding="utf-8") as f:
        await f.write(course_html)

    urls = extract_course_urls(course_html)
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        for name, url in urls.items():
            path_html = os.path.join(html_folder, f"{name}.html")
            if name == "syllabus_file":
                out_pdf = os.path.join(course_folder, "syllabus.pdf")
                if not os.path.exists(out_pdf):
                    try:
                        b = await fetch_bytes(url, cookie=cookie, client=client)
                        await save_bytes(os.path.join(html_folder, f"{name}.pdf"), b)
                        await save_bytes(out_pdf, b)
                    except Exception as e:
                        print("syllabus download failed", e)
            else:
                if not os.path.exists(path_html):
                    try:
                        txt = await fetch_text_playwright(url, cookie=cookie)
                        async with aiofiles.open(path_html, "w", encoding="utf-8") as f:
                            await f.write(txt)
                    except Exception as e:
                        print(f"{name} fetch failed", e)

    return {"course_html_path": os.path.join(html_folder, "course.html"), "urls": urls, "html_folder": html_folder, "course_folder": course_folder}

# ------------------ Parse course data ------------------
async def parse_course_data_from_folder(html_folder: str) -> Dict[str, Any]:
    data = {}
    scores_path = os.path.join(html_folder, "scores.html")
    if os.path.exists(scores_path):
        async with aiofiles.open(scores_path, encoding="utf-8") as f:
            txt = await f.read()
        data["scores"] = parse_scores(txt)
    my_lector = data.get("scores", {}).get("lector")
    files_path = os.path.join(html_folder, "files.html")
    if os.path.exists(files_path):
        async with aiofiles.open(files_path, encoding="utf-8") as f:
            txt = await f.read()
        data["materials"] = parse_files(txt, my_lector)
    groups_path = os.path.join(html_folder, "groups.html")
    if os.path.exists(groups_path):
        async with aiofiles.open(groups_path, encoding="utf-8") as f:
            txt = await f.read()
        data["groups"] = parse_groups(txt)
    return data

# ------------------ HTML Generation ------------------
def fmt_num(val):
    if isinstance(val, float) and val == int(val):
        return str(int(val))
    return str(val)

def get_grade_color(grade: float) -> str:
    if grade >= 91:
        return "#22c55e"
    elif grade >= 81:
        return "#84cc16"
    elif grade >= 71:
        return "#eab308"
    elif grade >= 61:
        return "#f97316"
    elif grade >= 51:
        return "#ef4444"
    else:
        return "#991b1b"

def get_percentage_color(percentage: float) -> str:
    return get_grade_color(percentage)

def generate_course_html(course: Dict[str, Any], data: Dict[str, Any]) -> str:
    scores = data.get("scores", {})
    materials = data.get("materials", [])
    grade = course["grade"]

    max_possible = 0
    for a in scores.get("assessments", []):
        if a.get("score") and a.get("max_points"):
            max_possible += a["max_points"]

    if isinstance(grade, (int, float)) and max_possible > 0:
        try:
            percentage = (float(grade) / max_possible) * 100
        except Exception:
            percentage = 0
        grade_color = get_percentage_color(percentage)
        grade_display = f"{fmt_num(grade)}/{fmt_num(max_possible)}"
        pct_badge = f'<span class="pct-badge" style="background: {grade_color}20; color: {grade_color}">{percentage:.0f}%</span>' if 0 < percentage < 100 else ""
    elif isinstance(grade, (int, float)):
        grade_color = get_grade_color(float(grade))
        grade_display = fmt_num(grade)
        pct_badge = ""
    else:
        grade_color = "#52525b"
        grade_display = str(grade)
        pct_badge = ""

    course_folder = os.path.join(COURSES_DIR, "".join(c for c in course["name"] if c.isalnum() or c in (" ", "-", "_")).strip())
    syllabus_path = os.path.join(course_folder, "syllabus.pdf")
    has_syllabus = os.path.exists(syllabus_path) or bool(data.get("syllabus_file"))

    assessments_html = ""
    for a in scores.get("assessments", []):
        raw_score = a["score"]
        max_points = a.get("max_points")
        if raw_score:
            try:
                score_val = float(raw_score.replace(",", "."))
                score_formatted = fmt_num(score_val)
            except Exception:
                score_formatted = raw_score
                score_val = None
            if max_points:
                score_display = f"{score_formatted}/{fmt_num(max_points)}"
                if score_val is not None:
                    percentage = (score_val / max_points) * 100
                    color = get_percentage_color(percentage)
                    score_class = f'" style="color: {color}'
                    pct = f'<span class="pct-badge" style="background: {color}20; color: {color}">{percentage:.0f}%</span>' if 0 < percentage < 100 else ""
                else:
                    score_class = ""
                    pct = ""
            else:
                score_display = score_formatted
                score_class = ""
                pct = ""
        else:
            score_display = "—"
            score_class = ""
            pct = ""
        assessments_html += f'<div class="assess"><span class="name">{a["component"]}</span>: <span class="score{score_class}">{score_display}</span> {pct}</div>'

    materials_html = ""
    for m in materials:
        url = m.get("url") or m.get("external_url") or "#"
        name = m.get("name") or "file"
        materials_html += f'<div class="material"><a href="{url}" target="_blank">{name}</a></div>'

    html = f"""
    <div class="course">
        <h2>{course['name']}</h2>
        <div class="grade" style="color:{grade_color}">{grade_display} {pct_badge}</div>
        <div class="assessments">{assessments_html}</div>
        <div class="materials">{materials_html}</div>
        {'<a href="' + syllabus_path + '" target="_blank">Syllabus PDF</a>' if has_syllabus else ''}
    </div>
    """
    return html

# ------------------ API Routes ------------------
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
            if r.status_code == 403:
                return JSONResponse(status_code=403, content={"error": f"Access forbidden: 403. URL: {url}"})
            r.raise_for_status()
            async def streamer():
                async for chunk in r.aiter_bytes():
                    yield chunk
            content_type = r.headers.get("content-type", "application/octet-stream")
            return StreamingResponse(streamer(), media_type=content_type)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

# ------------------ Static files ------------------
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
