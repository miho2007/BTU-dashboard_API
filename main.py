# main.py
import os
import urllib.parse
from typing import Optional, Tuple, List, Dict, Any
import aiofiles
from fastapi import FastAPI, Body, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

# ------------------ CONFIG ------------------
BASE_URL = "https://classroom.btu.edu.ge/en/student/me/courses"
HTML_DIR = "html"
COURSES_DIR = "courses"

os.makedirs(HTML_DIR, exist_ok=True)
os.makedirs(COURSES_DIR, exist_ok=True)

app = FastAPI(title="BTU Courses API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------ MODELS ------------------
class CookieInput(BaseModel):
    raw_cookie: str

# ------------------ HELPERS ------------------
def parse_num(txt: str):
    if not txt:
        return None
    txt = txt.strip().replace(",", ".")
    try:
        return float(txt)
    except:
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

def extract_course_urls(html: str) -> Dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    urls = {}
    tabs = soup.select_one("#course_tabs")
    if tabs:
        for link in tabs.find_all("a", href=True):
            href = link["href"]
            if "silabus" in href:
                urls["syllabus"] = href
            elif "groups" in href:
                urls["groups"] = href
            elif "scores" in href:
                urls["scores"] = href
            elif "files" in href:
                urls["files"] = href
    syllabus_file = soup.select_one('a[href*="courseSilabusFile"]')
    if syllabus_file:
        urls["syllabus_file"] = syllabus_file["href"]
    return urls

def parse_scores(html: str) -> Dict:
    import re
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
                    except:
                        pass
                data["assessments"].append({"component": component, "score": score or None, "max_points": max_points})
    return data

def parse_files(html: str, my_lector: Optional[str] = None) -> List[Dict]:
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
        ext_link = tds[1].select_one("a") if len(tds) > 1 else None
        ext_url = ext_link["href"] if ext_link else None
        if name:
            materials.append({"name": name, "url": url, "external_url": ext_url})
    return materials

def parse_groups(html: str) -> Dict:
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

# ------------------ PLAYWRIGHT FETCH ------------------
async def fetch_html(url: str, raw_cookie: str) -> str:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        # convert raw cookie
        cookies = []
        for part in raw_cookie.split(";"):
            if "=" not in part:
                continue
            name, value = part.strip().split("=", 1)
            cookies.append({
                "name": name.strip(),
                "value": value.strip(),
                "domain": ".classroom.btu.edu.ge",
                "path": "/",
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax"
            })
        await context.add_cookies(cookies)
        page = await context.new_page()
        await page.goto(url, timeout=120000)
        await page.wait_for_timeout(2000)
        html = await page.content()
        await browser.close()
        return html

async def fetch_course_pages(course: Dict[str, Any], raw_cookie: str) -> Dict[str, Any]:
    if not course.get("url"):
        return {}
    course_name = course["name"]
    safe_name = "".join(c for c in course_name if c.isalnum() or c in (" ", "-", "_")).strip()
    html_folder = os.path.join(HTML_DIR, safe_name)
    course_folder = os.path.join(COURSES_DIR, safe_name)
    os.makedirs(html_folder, exist_ok=True)
    os.makedirs(course_folder, exist_ok=True)

    course_html = await fetch_html(course["url"], raw_cookie)
    async with aiofiles.open(os.path.join(html_folder, "course.html"), "w", encoding="utf-8") as f:
        await f.write(course_html)

    urls = extract_course_urls(course_html)
    # fetch additional pages
    data = {}
    if "scores" in urls:
        scores_html = await fetch_html(urls["scores"], raw_cookie)
        data["scores"] = parse_scores(scores_html)
    if "files" in urls:
        files_html = await fetch_html(urls["files"], raw_cookie)
        my_lector = data.get("scores", {}).get("lector")
        data["materials"] = parse_files(files_html, my_lector)
    if "groups" in urls:
        groups_html = await fetch_html(urls["groups"], raw_cookie)
        data["groups"] = parse_groups(groups_html)
    return data

# ------------------ API ------------------
@app.post("/api/courses-full")
async def api_courses_full(input: CookieInput):
    raw_cookie = input.raw_cookie
    html = await fetch_html(BASE_URL, raw_cookie)
    courses, total_ects = parse_courses(html)

    full_data = []
    for course in courses:
        data = await fetch_course_pages(course, raw_cookie)
        full_data.append({"course": course, "data": data})

    return {"total_ects": total_ects, "courses": full_data}

# ------------------ HEALTH ------------------
@app.get("/health")
async def health():
    return {"status": "ok"}

