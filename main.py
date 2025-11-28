# api_courses_full.py
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import os, aiofiles
from typing import Dict, Any, List
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

app = FastAPI(title="BTU Courses Full API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_URL = "https://classroom.btu.edu.ge/en/student/me/courses"
HTML_DIR = "html"
COURSES_DIR = "courses"
os.makedirs(HTML_DIR, exist_ok=True)
os.makedirs(COURSES_DIR, exist_ok=True)

# ---------------- Parsing helpers ----------------

def parse_num(td_text: str):
    if not td_text:
        return None
    txt = td_text.strip().replace(",", ".")
    try:
        return float(txt)
    except Exception:
        return txt.strip()

def parse_courses(html: str):
    """Parse main courses table"""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.table.table-striped.table-bordered.table-hover.fluid")
    if not table:
        return [], None
    tbody = table.find("tbody")
    courses = []
    total_ects = None
    for tr in tbody.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) == 2 and not tds[0].get_text(strip=True):
            total_ects = parse_num(tds[1].get_text(strip=True))
            continue
        if len(tds) != 6:
            continue
        name_a = tds[2].find("a")
        courses.append({
            "name": name_a.get_text(strip=True) if name_a else tds[2].get_text(strip=True),
            "grade": parse_num(tds[3].get_text(strip=True)),
            "ects": parse_num(tds[5].get_text(strip=True)),
            "url": name_a["href"] if name_a and name_a.has_attr("href") else None,
        })
    return courses, total_ects

def parse_scores(html: str) -> Dict:
    """Extract scores/evaluations"""
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
            max_points = None
            max_match = re.search(r'max\.?\s*([\d.,]+)', component)
            if max_match:
                try:
                    max_points = float(max_match.group(1).replace(",", "."))
                except: pass
            data["assessments"].append({"component": component, "score": score or None, "max_points": max_points})
    return data

def parse_files(html: str, my_lector: str = None) -> List[Dict]:
    """Extract materials"""
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
        ext_link = tds[1].select_one("a") if len(tds) > 1 else None
        materials.append({
            "name": tds[0].get_text(strip=True),
            "url": file_link["href"] if file_link else None,
            "external_url": ext_link["href"] if ext_link else None
        })
    return materials

def parse_groups(html: str) -> Dict:
    """Extract group info"""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("#groups")
    groups = []
    if table:
        for tr in table.find_all("tr"):
            if "warning" in (tr.get("class") or []):
                continue
            text = tr.get_text(strip=True)
            if text and "Not found" not in text:
                groups.append(text)
    return {"groups": groups}

# ---------------- Playwright fetch ----------------

async def fetch_html(url: str, storage_state="auth.json"):
    """Fetch JS page via Playwright"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=storage_state)
        page = await context.new_page()
        await page.goto(url, timeout=120000)
        await page.wait_for_timeout(1000)
        html = await page.content()
        await browser.close()
        return html

# ---------------- Fetch full course data ----------------

async def fetch_course_full(course: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch all pages for one course and parse"""
    result = {"course": course, "scores": {}, "materials": [], "groups": {}}
    if not course.get("url"):
        return result
    html = await fetch_html(course["url"])
    # Extract course page URLs
    soup = BeautifulSoup(html, "html.parser")
    tabs = soup.select_one("#course_tabs")
    urls = {}
    if tabs:
        for link in tabs.find_all("a", href=True):
            href = link["href"]
            if "scores" in href: urls["scores"] = href
            if "files" in href: urls["files"] = href
            if "groups" in href: urls["groups"] = href
    # Scores
    if urls.get("scores"):
        scores_html = await fetch_html(urls["scores"])
        result["scores"] = parse_scores(scores_html)
    # Materials
    if urls.get("files"):
        files_html = await fetch_html(urls["files"])
        result["materials"] = parse_files(files_html, my_lector=result["scores"].get("lector"))
    # Groups
    if urls.get("groups"):
        groups_html = await fetch_html(urls["groups"])
        result["groups"] = parse_groups(groups_html)
    return result

# ---------------- API Endpoint ----------------

@app.get("/api/courses-full")
async def api_courses_full():
    if not os.path.exists("auth.json"):
        raise HTTPException(status_code=400, detail="auth.json missing. Set cookie first.")
    main_html = await fetch_html(BASE_URL)
    courses, total_ects = parse_courses(main_html)
    all_data = []
    for course in courses:
        data = await fetch_course_full(course)
        all_data.append(data)
    return {"total_ects": total_ects, "courses": all_data}
