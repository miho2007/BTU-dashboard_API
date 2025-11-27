# main.py
import os
import shutil
import urllib.parse
from typing import Optional, Tuple, List, Dict, Any

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request, Response, HTTPException, Body, Query
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader
import aiofiles
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="BTU Courses - FastAPI proxy & scraper")


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # or your frontend URL
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)







# Configuration - change if needed
BASE_URL = "https://classroom.btu.edu.ge/en/student/me/courses"
TEMPLATES_DIR = "templates"
TEMPLATE_NAME = "template.html"
HTML_DIR = "html"
COURSES_DIR = "courses"
INDEX_HTML = "index.html"

# In-memory cookie (can be set via API or env)
COOKIE = os.getenv("BTU_COOKIE", None)

# Ensure folders exist
os.makedirs(HTML_DIR, exist_ok=True)
os.makedirs(COURSES_DIR, exist_ok=True)
os.makedirs(TEMPLATES_DIR, exist_ok=True)

# Jinja2 environment
jinja_env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=True)

# --- Utility network functions (async using httpx) ---
async def fetch_text(url: str, cookie: Optional[str] = None, client: Optional[httpx.AsyncClient] = None) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": "https://classroom.btu.edu.ge/ge/student/me/courses",
        "X-Requested-With": "XMLHttpRequest",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Sec-Ch-Ua": '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"',
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Cookie": COOKIE or "" # make sure this includes argus_session + cf_clearance
    }
    if cookie:
        headers["Cookie"] = cookie
    close_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        close_client = True
    try:
        r = await client.get(url, headers=headers)
        r.raise_for_status()
        return r.text
    finally:
        if close_client:
            await client.aclose()


async def fetch_bytes(url: str, cookie: Optional[str] = None, client: Optional[httpx.AsyncClient] = None) -> bytes:
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
    if cookie:
        headers["Cookie"] = cookie
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

# --- Parsing helpers (ported from your script) ---
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

        # Ensure absolute URL if relative
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


import re
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
        # convert to absolute
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


# Helper to write files async
async def save_bytes(path: str, data: bytes):
    dirpath = os.path.dirname(path)
    if dirpath and not os.path.exists(dirpath):
        os.makedirs(dirpath, exist_ok=True)
    async with aiofiles.open(path, "wb") as f:
        await f.write(data)


# --- High-level flow functions ---
async def fetch_course_pages(course: Dict[str, Any], cookie: Optional[str] = None) -> Dict[str, Any]:
    """Fetch course main page and detect subpages (syllabus, files, scores, groups). Write course html to html/<course_name>/course.html"""
    if not course.get("url"):
        return {}
    course_name = course["name"]
    safe_name = "".join(c for c in course_name if c.isalnum() or c in (" ", "-", "_")).strip()
    html_folder = os.path.join(HTML_DIR, safe_name)
    course_folder = os.path.join(COURSES_DIR, safe_name)
    os.makedirs(html_folder, exist_ok=True)
    os.makedirs(course_folder, exist_ok=True)
    os.makedirs(os.path.join(course_folder, "material"), exist_ok=True)

    course_html = await fetch_text(course["url"], cookie=cookie)
    # save course.html
    async with aiofiles.open(os.path.join(html_folder, "course.html"), "w", encoding="utf-8") as f:
        await f.write(course_html)

    urls = extract_course_urls(course_html)
    # fetch subpages according to rules in your script
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        for name, url in urls.items():
            if name == "syllabus_file":
                # syllabus pdf - skip if exists in course_folder
                out_pdf = os.path.join(course_folder, "syllabus.pdf")
                if not os.path.exists(out_pdf):
                    try:
                        b = await fetch_bytes(url, cookie=cookie, client=client)
                        await save_bytes(os.path.join(html_folder, f"{name}.pdf"), b)
                        await save_bytes(out_pdf, b)
                    except Exception as e:
                        # ignore download errors, continue
                        print("syllabus download failed", e)
            elif name == "scores":
                # always refetch
                try:
                    txt = await fetch_text(url, cookie=cookie, client=client)
                    async with aiofiles.open(os.path.join(html_folder, f"{name}.html"), "w", encoding="utf-8") as f:
                        await f.write(txt)
                except Exception as e:
                    print("scores fetch failed", e)
            elif name == "files":
                try:
                    txt = await fetch_text(url, cookie=cookie, client=client)
                    async with aiofiles.open(os.path.join(html_folder, f"{name}.html"), "w", encoding="utf-8") as f:
                        await f.write(txt)
                except Exception as e:
                    print("files fetch failed", e)
            else:
                # groups or syllabus - don't refetch if exists
                path_html = os.path.join(html_folder, f"{name}.html")
                if not os.path.exists(path_html):
                    try:
                        txt = await fetch_text(url, cookie=cookie, client=client)
                        async with aiofiles.open(path_html, "w", encoding="utf-8") as f:
                            await f.write(txt)
                    except Exception as e:
                        print(f"{name} fetch failed", e)

    return {"course_html_path": os.path.join(html_folder, "course.html"), "urls": urls, "html_folder": html_folder, "course_folder": course_folder}


async def parse_course_data_from_folder(html_folder: str) -> Dict[str, Any]:
    """Read scores.html, files.html, groups.html from html_folder (if present) and parse."""
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


# HTML generation functions (uses the same template strings as your script)
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
    if percentage >= 91:
        return "#22c55e"
    elif percentage >= 81:
        return "#84cc16"
    elif percentage >= 71:
        return "#eab308"
    elif percentage >= 61:
        return "#f97316"
    elif percentage >= 51:
        return "#ef4444"
    else:
        return "#991b1b"


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
        if 0 < percentage < 100:
            pct_badge = f'<span class="pct-badge" style="background: {grade_color}20; color: {grade_color}">{percentage:.0f}%</span>'
        else:
            pct_badge = ""
    elif isinstance(grade, (int, float)):
        grade_color = get_grade_color(float(grade))
        grade_display = fmt_num(grade)
        pct_badge = ""
    else:
        grade_color = "#52525b"
        grade_display = str(grade)
        pct_badge = ""

    # safe course folder used for file links
    course_folder = os.path.join(COURSES_DIR, "".join(c for c in course["name"] if c.isalnum() or c in (" ", "-", "_")).strip())
    syllabus_path = os.path.join(course_folder, "syllabus.pdf")
    has_syllabus = os.path.exists(syllabus_path) or bool(data.get("syllabus_file"))

    # Build assessments HTML
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
                    if 0 < percentage < 100:
                        pct = f'<span class="pct-badge" style="background: {color}20; color: {color}">{percentage:.0f}%</span>'
                    else:
                        pct = ""
                else:
                    score_class = ""
                    pct = ""
            else:
                score_display = score_formatted
                score_class = ""
                pct = ""
        else:
            score_display = "—"
            score_class = " empty"
            pct = ""
        name = a["component"]
        if "(" in name:
            name = name.split("(")[0].strip()
        assessments_html += f'<span class="assessment"><span class="assessment-name">{name}</span><span class="assessment-score{score_class}">{score_display}</span>{pct}</span>'

    syllabus_html = ""
    if has_syllabus and data.get("syllabus_file"):
        # render proxy link - frontend will request /api/proxy?url=<encoded>
        syllabus_html = f'<a href="/api/proxy?url={urllib.parse.quote(data["syllabus_file"], safe="")}" class="syllabus-link" target="_blank">Syllabus</a>'
    elif os.path.exists(syllabus_path):
        syllabus_html = f'<a href="/{syllabus_path.replace(os.sep, "/")}" class="syllabus-link" target="_blank">Syllabus</a>'

    materials_html = ""
    if materials:
        material_links = ""
        for m in materials:
            if m["url"]:
                prox = f'/api/proxy?url={urllib.parse.quote(m["url"], safe="")}'
                material_links += f'<a href="{prox}" class="material" target="_blank">{m["name"]}</a>'
        materials_html = f'''<div class="materials-section">
            <div class="materials-toggle"><span class="arrow">▶</span> Materials ({len(materials)})</div>
            <div class="materials">{material_links}</div>
        </div>'''

    return f'''<div class="course">
    <div class="course-header">
        <div class="course-info">
            <div class="course-name">{course['name']}</div>
            <div class="course-meta">Group {scores.get('group', '?')} · {scores.get('lector', 'Unknown')}</div>
        </div>
        {syllabus_html}
        <span class="ects">{int(course['ects']) if isinstance(course['ects'], (float, int)) else course['ects']} ECTS</span>
        <div class="grade" style="color: {grade_color}">{grade_display}{pct_badge}</div>
    </div>
    <div class="assessments">{assessments_html}</div>
    {materials_html}
</div>'''


def generate_summary_html(courses_data: List[tuple], total_ects: Optional[float]) -> str:
    total_score = 0
    total_max_possible = 0
    total_ects_earned = 0
    course_count = len(courses_data)
    course_percentages = []
    for course, data in courses_data:
        grade = course["grade"]
        ects = course["ects"]
        if isinstance(grade, (int, float)):
            total_score += grade
        course_max = 0
        for a in data.get("scores", {}).get("assessments", []):
            if a.get("score") and a.get("max_points"):
                course_max += a["max_points"]
        total_max_possible += course_max
        if isinstance(grade, (int, float)) and course_max > 0 and isinstance(ects, (int, float)):
            pct = (grade / course_max) * 100
            course_percentages.append((pct, ects))
            total_ects_earned += ects
    weighted_gpa = 0
    for pct, ects in course_percentages:
        if pct >= 91:
            gpa_points = 4.0
        elif pct >= 81:
            gpa_points = 3.0
        elif pct >= 71:
            gpa_points = 2.0
        elif pct >= 61:
            gpa_points = 1.0
        elif pct >= 51:
            gpa_points = 0.5
        else:
            gpa_points = 0.0
        weighted_gpa += gpa_points * ects
    gpa = weighted_gpa / total_ects_earned if total_ects_earned > 0 else 0
    gpa_pct = (gpa / 4.0) * 100
    gpa_color = get_percentage_color(gpa_pct)
    score_pct = (total_score / total_max_possible * 100) if total_max_possible > 0 else 0
    score_color = get_percentage_color(score_pct)
    if total_max_possible > 0 and 0 < score_pct < 100:
        score_pct_badge = f'<span class="pct-badge" style="background: {score_color}20; color: {score_color}">{score_pct:.0f}%</span>'
    else:
        score_pct_badge = ""
    return f'''<div class="summary">
    <div class="summary-item">
        <div class="summary-label">GPA</div>
        <div class="summary-value" style="color: {gpa_color}">{gpa:.2f}</div>
    </div>
    <div class="summary-item">
        <div class="summary-label">Total Score</div>
        <div class="summary-value" style="color: {score_color}">{fmt_num(total_score)}/{fmt_num(total_max_possible)} {score_pct_badge}</div>
    </div>
    <div class="summary-item">
        <div class="summary-label">Courses</div>
        <div class="summary-value" style="color: #a78bfa">{course_count}</div>
    </div>
    <div class="summary-item">
        <div class="summary-label">ECTS</div>
        <div class="summary-value" style="color: #a78bfa">{fmt_num(total_ects_earned)}</div>
    </div>
</div>'''


def generate_dashboard_html(courses_data: List[tuple], total_ects: Optional[float] = None) -> str:
    # Read template
    template_path = os.path.join(TEMPLATES_DIR, TEMPLATE_NAME)
    if not os.path.exists(template_path):
        raise FileNotFoundError(f"Template not found at {template_path}. Place your HTML template there.")
    template = jinja_env.get_template(TEMPLATE_NAME).render()  # we'll replace placeholders manually as in your script

    # build courses html
    courses_html = ""
    for course, data in courses_data:
        courses_html += generate_course_html(course, data)
    summary_html = generate_summary_html(courses_data, total_ects)
    rendered = template.replace("{{COURSES}}", courses_html).replace("{{SUMMARY}}", summary_html)
    return rendered


# --- FastAPI endpoints ---

@app.post("/api/set_cookie")
async def set_cookie(cookie: str = Body(..., embed=True)):
    """Set session cookie for future requests (kept in memory)."""
    global COOKIE
    COOKIE = cookie
    return {"status": "ok"}


@app.post("/api/fetch")
async def api_fetch(url: str = Body(...), binary: bool = Body(False)):
    """
    Fetch a remote URL using the server cookie (or provided cookie param).
    Body: { "url": "...", "binary": false }
    """
    if binary:
        b = await fetch_bytes(url, cookie=COOKIE)
        return Response(content=b, media_type="application/octet-stream")
    else:
        txt = await fetch_text(url, cookie=COOKIE)
        return HTMLResponse(txt)


@app.get("/api/proxy")
async def api_proxy(url: str = Query(...)):
    """
    Proxy a file download. Streams bytes from remote server to client.
    Example: /api/proxy?url=<encoded full url>
    """
    if not url:
        raise HTTPException(status_code=400, detail="Missing url")
    # use httpx streaming
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
    if COOKIE:
        headers["Cookie"] = COOKIE
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        r = await client.get(url, headers=headers, stream=True)
        if r.status_code >= 400:
            return Response(content=await r.aread(), status_code=r.status_code)
        content_type = r.headers.get("content-type", "application/octet-stream")
        # Create generator for streaming
        async def streamer():
            async for chunk in r.aiter_bytes():
                yield chunk
        return StreamingResponse(streamer(), media_type=content_type)

@app.get("/api/courses")
async def api_courses():
    """Return parsed courses list from the main page (does not fetch subpages)."""
    html = await fetch_text(BASE_URL, cookie=COOKIE)
    courses, total_ects = parse_courses(html)
    return {"courses": courses, "total_ects": total_ects}


@app.post("/api/generate")
async def api_generate(refetch: bool = Body(False)):
    """
    Full flow: fetch courses, then for each course fetch course pages (scores/files), parse them,
    download materials (into courses/<name>/material) and write index.html to disk. Returns a simple status.
    """
    html = await fetch_text(BASE_URL, cookie=COOKIE)
    courses, total_ects = parse_courses(html)

    courses_data = []
    idx = 0
    for course in courses:
        idx += 1
        # fetch course pages (and save relevant html & pdfs)
        res = await fetch_course_pages(course, cookie=COOKIE)
        # parse data from saved html folder
        data = await parse_course_data_from_folder(res.get("html_folder", ""))
        # download materials now (if any)
        materials = data.get("materials", [])
        course_folder = res.get("course_folder")
        if materials and course_folder:
            for m in materials:
                if not m.get("url"):
                    continue
                filename = os.path.basename(urllib.parse.urlparse(m["url"]).path)
                outpath = os.path.join(course_folder, "material", filename)
                if os.path.exists(outpath):
                    continue
                try:
                    b = await fetch_bytes(m["url"], cookie=COOKIE)
                    await save_bytes(outpath, b)
                except Exception as e:
                    print("Failed to download", m.get("url"), e)
        courses_data.append((course, data))

    # generate dashboard HTML and save to INDEX_HTML
    dashboard_html = generate_dashboard_html(courses_data, total_ects)
    async with aiofiles.open(INDEX_HTML, "w", encoding="utf-8") as f:
        await f.write(dashboard_html)

    return {"status": "ok", "courses_count": len(courses_data)}


# Serve static files and index.html
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", response_class=HTMLResponse)
async def root():
    # If index.html exists in project root (generated), serve it; otherwise inform to generate
    if os.path.exists(INDEX_HTML):
        async with aiofiles.open(INDEX_HTML, encoding="utf-8") as f:
            return HTMLResponse(await f.read())
    else:
        return HTMLResponse("<html><body><h3>Index not generated yet.</h3><p>Call POST /api/generate to produce dashboard (set cookie first via /api/set_cookie).</p></body></html>")
