"""LinkedIn public (guest) job search - no login, no account risk."""
import random
import time
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

SEARCH_URL = (
    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
    "?keywords={kw}&location={loc}&geoId={geo}&f_TPR=r{secs}&sortBy=DD&start={start}"
)
DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,he;q=0.8",
}

session = requests.Session()
session.headers.update(HEADERS)


def _get(url: str, retries: int = 3) -> str | None:
    for attempt in range(retries):
        try:
            r = session.get(url, timeout=20)
            if r.status_code == 200:
                return r.text
            if r.status_code == 429:  # rate limited -> back off
                time.sleep(30 * (attempt + 1))
                continue
            return None
        except requests.RequestException:
            time.sleep(5)
    return None


def _pause(a: float = 1.5, b: float = 3.5) -> None:
    time.sleep(random.uniform(a, b))


def _text(el) -> str:
    return el.get_text(" ", strip=True) if el else ""


def search(keyword: str, location: str, geo_id: str, hours: int, pages: int) -> list[dict]:
    jobs = []
    for page in range(pages):
        html = _get(SEARCH_URL.format(
            kw=quote(keyword), loc=quote(location), geo=geo_id,
            secs=hours * 3600, start=page * 10,
        ))
        if not html:
            break
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select("div.base-search-card, li > div[data-entity-urn]")
        if not cards:
            break
        for c in cards:
            urn = c.get("data-entity-urn", "")
            job_id = urn.rsplit(":", 1)[-1]
            if not job_id.isdigit():
                continue
            time_el = c.select_one("time")
            jobs.append({
                "job_id": job_id,
                "title": _text(c.select_one(".base-search-card__title")),
                "company": _text(c.select_one(".base-search-card__subtitle")),
                "location": _text(c.select_one(".job-search-card__location")),
                "posted": time_el.get("datetime", "") if time_el else "",
                "posted_text": _text(time_el),
                "url": f"https://www.linkedin.com/jobs/view/{job_id}/",
            })
        if len(cards) < 5:
            break
        _pause()
    return jobs


def details(job_id: str) -> dict:
    html = _get(DETAIL_URL.format(job_id=job_id))
    if not html:
        return {"description": "", "seniority": "", "applicants": ""}
    soup = BeautifulSoup(html, "html.parser")
    desc = soup.select_one(".show-more-less-html__markup") or soup.select_one(".description__text")
    criteria = {}
    for item in soup.select(".description__job-criteria-item"):
        k = _text(item.select_one(".description__job-criteria-subheader")).lower()
        criteria[k] = _text(item.select_one(".description__job-criteria-text"))
    return {
        "description": desc.get_text("\n", strip=True) if desc else "",
        "seniority": criteria.get("seniority level", ""),
        "applicants": _text(soup.select_one(".num-applicants__caption")),
    }
