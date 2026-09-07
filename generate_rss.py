#!/usr/bin/env python3
"""Generate an RSS feed for PAGASA regional advisories.

v3.2 — drops FUTURE-DATED items (AM/PM misparses).

Problem found: PAGASA occasionally shows a mistyped time (e.g. "11:50 PM"
instead of "11:50 AM"). Parsed as 11:50 PM, that item got a pubDate ~8h in the
FUTURE relative to when it was scraped. Because the RSS trigger only fires for
items with pubDate greater than the highest seen, this phantom "future" item
jumped the watermark, causing the genuine later-but-earlier-timestamped
advisory (e.g. the real 2:00 PM one) to be treated as old and SKIPPED.

Fix: if an item's parsed issued time is in the future (beyond a small grace
window vs. the scrape time), we treat it as a misparse and SKIP the item.

Retains:
- cache-busting fetch (always get live page, not stale CDN copy)
- unique/monotonic pubDates (advisory number as seconds offset)
- undated items pinned to a stable past date
- "no advisory" placeholders dropped

Usage:
    python generate_rss.py <slug>        e.g.  python generate_rss.py ncrprsd
"""

import argparse
import hashlib
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from html import unescape
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from lxml import etree as ET

PH_TZ = ZoneInfo("Asia/Manila")
_STABLE_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)

# Grace window: an issued time more than this far in the FUTURE vs. scrape time
# is treated as a misparse (e.g. AM read as PM) and the item is skipped.
_FUTURE_GRACE = timedelta(hours=2)

_ISSUED_RE = re.compile(
    r"Issued\s*at\s*:?\s*(?P<body>.+?)(?:<|$)", re.IGNORECASE | re.DOTALL
)
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*([AaPp][Mm])")
_DATE_RE = re.compile(
    r"(\d{1,2})\s+"
    r"(January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\,?\s+(\d{4})",
    re.IGNORECASE,
)
_PLACEHOLDER_RE = re.compile(
    r"there\s+is\s+no\b.*\bissued|no\s+.*\bwarning\s+issued", re.IGNORECASE
)
_NUMBER_RE = re.compile(r"(?:No|Number)\.?\s*(\d+)", re.IGNORECASE)

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def normalize_html(html):
    if not html:
        return ""
    html = re.sub(r"\s+", " ", html.strip())
    html = re.sub(r"<br\s*/?>", "<br />", html, flags=re.IGNORECASE)
    html = html.replace("</br>", "")
    return html


def is_placeholder(text):
    return bool(_PLACEHOLDER_RE.search(text or ""))


def parse_issued_date(text):
    if not text:
        return None
    match = _ISSUED_RE.search(text)
    search_space = match.group("body") if match else text
    date_match = _DATE_RE.search(search_space) or _DATE_RE.search(text)
    if not date_match:
        return None
    day = int(date_match.group(1))
    month = _MONTHS[date_match.group(2).lower()]
    year = int(date_match.group(3))
    hour, minute = 0, 0
    time_match = _TIME_RE.search(search_space) or _TIME_RE.search(text)
    if time_match:
        hour = int(time_match.group(1)) % 12
        minute = int(time_match.group(2))
        if time_match.group(3).lower() == "pm":
            hour += 12
    try:
        return datetime(year, month, day, hour, minute, tzinfo=PH_TZ)
    except ValueError:
        return None


def stable_date_for(*parts):
    digest = hashlib.sha1("||".join(p for p in parts if p).encode()).hexdigest()
    offset = int(digest[:6], 16) % (365 * 24 * 3600)
    return _STABLE_EPOCH + timedelta(seconds=offset)


def make_guid(*parts):
    return hashlib.sha1("||".join(p for p in parts if p).encode()).hexdigest()


def add_pubdate(item, pub_dt):
    ET.SubElement(item, "pubDate").text = format_datetime(
        pub_dt.astimezone(timezone.utc)
    )


def add_items(soup, channel, div_id, category, slug, now):
    div = soup.find("div", id=div_id)
    if not div:
        return

    if div_id == "special-forecasts":
        for link in div.find_all("a"):
            href = link.get("href")
            spans = [s.get_text(strip=True) for s in link.find_all("span")]
            parts = [p for p in spans if p]
            if parts:
                html = "<br />".join(parts)
                title = next(
                    (p for p in parts if not re.match(r"(?i)\s*issued\s*at", p)),
                    parts[0],
                )
            else:
                html = link.get_text(separator="<br />", strip=True)
                title = link.get_text(strip=True)

            if is_placeholder(html):
                continue

            parsed = parse_issued_date(link.get_text(" ", strip=True))
            # Skip future-dated (misparsed) items.
            if parsed is not None and parsed.astimezone(timezone.utc) > now + _FUTURE_GRACE:
                continue

            item = ET.SubElement(channel, "item")
            item_title = f"{category}: {title}" if title else category
            ET.SubElement(item, "title").text = item_title
            if href:
                if href.startswith("/"):
                    href = f"https://www.pagasa.dost.gov.ph{href}"
                ET.SubElement(item, "link").text = href
            if html:
                html = unescape(html)
                desc = ET.SubElement(item, "description")
                desc.text = ET.CDATA(html)

            pub_dt = parsed if parsed is not None else stable_date_for(item_title, html)
            add_pubdate(item, pub_dt)

            guid = ET.SubElement(
                item, "guid", isPermaLink="true" if href else "false"
            )
            guid.text = href if href else make_guid(item_title, html)
            ET.SubElement(item, "category").text = category
    else:
        for entry in div.find_all("div"):
            html = entry.decode_contents()
            html = normalize_html(html)
            if not html.strip():
                continue
            if is_placeholder(html):
                continue

            parsed = parse_issued_date(entry.get_text(" ", strip=True))
            # Skip future-dated (misparsed) items so they can't jump the
            # RSS trigger's watermark and hide genuine advisories.
            if parsed is not None and parsed.astimezone(timezone.utc) > now + _FUTURE_GRACE:
                continue

            match = _NUMBER_RE.search(html)
            number = match.group(1) if match else None
            title = (
                f"{category} No. {number} #{slug.upper()}"
                if number else f"{category} #{slug.upper()}"
            )

            item = ET.SubElement(channel, "item")
            ET.SubElement(item, "title").text = title
            html = unescape(html)
            desc = ET.SubElement(item, "description")
            desc.text = ET.CDATA(html)

            if parsed is None:
                pub_dt = stable_date_for(title, html)
            else:
                pub_dt = parsed
                if number is not None:
                    pub_dt = pub_dt + timedelta(seconds=int(number))
            add_pubdate(item, pub_dt)

            guid = ET.SubElement(item, "guid", isPermaLink="false")
            guid.text = make_guid(title, html)
            ET.SubElement(item, "category").text = category


def find_page_issued_date(soup):
    return parse_issued_date(soup.get_text(" ", strip=True))


def main(slug: str) -> None:
    base = f"https://www.pagasa.dost.gov.ph/regional-forecast/{slug}"
    try:
        response = requests.get(
            base,
            timeout=30,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Cache-Control": "no-cache, no-store, max-age=0",
                "Pragma": "no-cache",
            },
            params={"_": int(time.time())},
        )
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"Error fetching data from {base}: {e}")
        return

    soup = BeautifulSoup(response.content, "html.parser")
    now = datetime.now(timezone.utc)
    page_dt = find_page_issued_date(soup) or now

    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = f"PAGASA {slug.upper()} Advisories"
    ET.SubElement(channel, "link").text = base
    ET.SubElement(channel, "description").text = (
        f"Aggregated rainfall, thunderstorm, and special forecasts from "
        f"PAGASA {slug.upper()}"
    )
    ET.SubElement(channel, "language").text = "en-ph"
    ET.SubElement(channel, "pubDate").text = format_datetime(page_dt)
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(now)

    add_items(soup, channel, "rainfalls", "Rainfall Advisory", slug, now)
    add_items(soup, channel, "thunderstorms", "Thunderstorm Advisory", slug, now)
    add_items(soup, channel, "special-forecasts", "Special Forecast", slug, now)

    try:
        ET.indent(rss, space="  ")
        tree = ET.ElementTree(rss)
        tree.write(f"{slug}.rss", encoding="utf-8", xml_declaration=True)
        print(f"Successfully generated {slug}.rss")
    except Exception as e:
        print(f"Error writing RSS file: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate PAGASA RSS feed")
    parser.add_argument("slug", help="PAGASA regional forecast slug")
    args = parser.parse_args()
    main(args.slug)
