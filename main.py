# main.py
import os
import shutil
import urllib.parse
from typing import Optional, Tuple, List, Dict, Any
import re
import aiofiles
from fastapi import FastAPI, Body, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from jinja2 import Environment, FileSystemLoader
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from pydantic import BaseModel



app = FastAPI(title="BTU Courses - FastAPI Playwright Scraper")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # or your frontend URL
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- Configuration ----------------
class CookieInput(BaseModel):
    raw_cookie: str

BASE_URL = "https://classroom.btu.edu.ge/en/student/me/courses"
HTML_DIR = "html"
COURSES_DIR = "courses"
TEMPLATES_DIR = "templates"

os.makedirs(HTML_DIR, exist_ok=True)
os.makedirs(COURSES_DIR, exist_ok=True)
os.makedirs(TEMPLATES_DIR, exist_ok=True)

jinja_env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=True)

# ---------------- Playwright fetch ----------------




        
        
async def fetch_text_playwright(
    url: str,
    raw_cookie: Optional[str] = None,
    storage_state: Optional[str] = None
) -> str:
    """
    Fetch JS-rendered page using Playwright.
    - Uses `storage_state` if provided (preferred).
    - Falls back to `raw_cookie` if provided.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)  # headless for server

        context_args = {}
        if storage_state and os.path.exists(storage_state):
            context_args["storage_state"] = storage_state

        context = await browser.new_context(**context_args)

        # If no storage_state, optionally set raw cookies
        if not context_args and raw_cookie:
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
        await page.goto(url, timeout=120000)  # 2 minutes

        # Wait until at least one grade cell is non-zero
        try:
            await page.wait_for_function("""
            () => {
                const tds = document.querySelectorAll('table tbody tr td:nth-child(4)');
                return Array.from(tds).some(td => {
                    const txt = td.textContent.trim().replace(',', '.');
                    return !isNaN(parseFloat(txt)) && parseFloat(txt) !== 0;
                });
            }
            """, timeout=30000)  # wait up to 30 seconds
        except:
            print("Warning: grades may still be zero")

        # Extra short delay to ensure lazy-loaded content is ready
        await page.wait_for_timeout(2000)

        html = await page.content()
        await browser.close()
        return html



# ---------------- File saving ----------------
async def save_bytes(path: str, data: bytes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    async with aiofiles.open(path, "wb") as f:
        await f.write(data)

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

# ---------------- Fetch course pages ----------------


def parse_courses(html: str) -> Tuple[List[Dict[str, Any]], Optional[float]]:
    """
    Parse course table from BTU Classroom HTML. Robust to minor HTML changes.
    Returns list of courses and total ECTS.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if not table:
        print("Warning: No table found in HTML")
        return [], None

    tbody = table.find("tbody")
    if not tbody:
        print("Warning: Table has no tbody")
        return [], None

    courses = []
    total_ects = None

    for tr in tbody.find_all("tr"):
        tds = tr.find_all("td")
        tds_text = [td.get_text(strip=True) for td in tds]
        # Debug: print row content
        # print("Row TDS:", tds_text)

        # Detect total ECTS row
        if len(tds) == 2 and not tds_text[0]:
            try:
                total_ects = float(tds_text[1].replace(",", "."))
            except:
                total_ects = None
            continue

        # Skip rows that don’t look like courses
        if len(tds) < 6:
            continue

        # Get course name
        name_a = tds[2].find("a")
        name = name_a.get_text(strip=True) if name_a else tds_text[2]

        # Grade and ECTS
        grade_txt = tds_text[3]
        ects_txt = tds_text[5]

        try:
            grade = float(grade_txt.replace(",", ".")) if grade_txt.replace(",", ".").replace(".", "").isdigit() else None
        except:
            grade = None

        try:
            ects = float(ects_txt.replace(",", ".")) if ects_txt.replace(",", ".").replace(".", "").isdigit() else None
        except:
            ects = None

        # Course URL
        url = name_a["href"] if name_a and name_a.has_attr("href") else None
        if url and not urllib.parse.urlparse(url).netloc:
            url = urllib.parse.urljoin(BASE_URL, url)

        courses.append({
            "name": name,
            "grade": grade if grade is not None else 0,
            "ects": ects if ects is not None else 0,
            "url": url
        })

    print(f"Parsed {len(courses)} courses, total ECTS: {total_ects}")
    return courses, total_ects





async def fetch_course_pages(course: Dict[str, Any], storage_state: Optional[str] = "auth.json") -> Dict[str, Any]:
    if not course.get("url"):
        return {}
    course_name = course["name"]
    safe_name = "".join(c for c in course_name if c.isalnum() or c in (" ", "-", "_")).strip()
    html_folder = os.path.join(HTML_DIR, safe_name)
    course_folder = os.path.join(COURSES_DIR, safe_name)
    os.makedirs(html_folder, exist_ok=True)
    os.makedirs(course_folder, exist_ok=True)

    course_html = await fetch_text_playwright(course["url"], storage_state=storage_state)
    async with aiofiles.open(os.path.join(html_folder, "course.html"), "w", encoding="utf-8") as f:
        await f.write(course_html)

    return {"course_html_path": os.path.join(html_folder, "course.html"), "html_folder": html_folder, "course_folder": course_folder}

# ---------------- API routes ----------------
@app.get("/api/courses")
async def api_courses():
    if not os.path.exists("auth.json"):
        raise HTTPException(status_code=400, detail="Auth file missing. Login manually first to create auth.json")
    
    html = await fetch_text_playwright(BASE_URL, storage_state="auth.json")

    courses, total_ects = parse_courses(html)

    courses_data = []
    for course in courses:
        await fetch_course_pages(course, storage_state="auth.json")
        courses_data.append(course)

    return {"courses": courses, "total_ects": total_ects, "fetched_courses": len(courses)}

@app.post("/api/fetch")
async def api_fetch(url: str = Body(...), binary: bool = Body(False)):
    try:
        if binary:
            import httpx
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                r = await client.get(url)
                r.raise_for_status()
                return Response(content=r.content, media_type="application/octet-stream")
        else:
            html = await fetch_text_playwright(url, storage_state="auth.json")
            return HTMLResponse(html)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})



from pydantic import BaseModel

class CookieInput(BaseModel):
    raw_cookie: str

@app.post("/api/set-cookie")
async def set_cookie(input: CookieInput):
    """
    Instead of manually parsing cookies, we launch a headless browser, set the cookie,
    and save the full storage_state (cookies + localStorage) for Playwright.
    """
    raw_cookie = input.raw_cookie

    try:
        # Convert raw cookie string into Playwright format
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
                "httpOnly": False,
                "secure": True,
                "sameSite": "Lax"
            })

        # Launch headless browser and set cookies
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            await context.add_cookies(cookies)

            page = await context.new_page()
            await page.goto("https://classroom.btu.edu.ge/en/student/me/courses", timeout=60000)

            # Wait a bit to ensure page fully loads
            await page.wait_for_timeout(2000)

            # Save storage_state with all cookies + localStorage
            await context.storage_state(path="auth.json")
            await browser.close()

        return {"status": "ok", "message": "auth.json created successfully"}

    except Exception as e:
        return {"status": "error", "message": str(e)}


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

class FetchInput(BaseModel):
    url: str
    binary: bool = False

@app.post("/api/fetch")
async def api_fetch(input: FetchInput):
    url = input.url
    binary = input.binary

    # Check if auth.json exists
    if not os.path.exists("auth.json"):
        raise HTTPException(status_code=400, detail="Auth file missing. Please set cookie first.")

    try:
        if binary:
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                r = await client.get(url)
                r.raise_for_status()
                return Response(content=r.content, media_type="application/octet-stream")
        else:
            # Use Playwright for JS-rendered pages
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(storage_state="auth.json")
                page = await context.new_page()
                await page.goto(url, timeout=60000)
                html = await page.content()
                await browser.close()
                return HTMLResponse(html)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

# ---------------- Manual login helper ----------------
async def create_storage_state():
    """Run this once manually to generate auth.json for Playwright"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(BASE_URL)
        print("Login manually in the browser, then press Enter here...")
        input()
        await context.storage_state(path="auth.json")
        await browser.close()

# ---------------- Run server ----------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)
