#!/usr/bin/env python3
"""
SF Board of Supervisors resolution scraper, v2 -- InSite + PDF attachments.

Discovery: SF's Legistar pages carry NO inline legislation text. The text
lives in PDF attachments named "Leg Ver1..N" / "Leg Final" on each matter's
detail page. Pipeline:

  search (works)  ->  detail page  ->  find best "Leg *" attachment
                 ->  download PDF  ->  extract text  ->  clean  ->  save

Setup:
  uv add lxml scrapelib pytz requests pdfplumber
  uv add "scraper-legistar @ git+https://github.com/opencivicdata/python-legistar-scraper"

Usage:
  uv run scrape_sf_insite.py
"""

import csv
import io
import json
import re
import time
import urllib3
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import pdfplumber
from legistar.bills import LegistarBillScraper

urllib3.disable_warnings()


class SFBillScraper(LegistarBillScraper):
    LEGISLATION_URL = "https://sfgov.legistar.com/Legislation.aspx"
    BASE_URL = "https://sfgov.legistar.com"
    TIMEZONE = "US/Pacific"


WINDOWS = {
    "2022": (date(2021, 1, 1), date(2022, 6, 30), date(2022, 6, 1), date(2022, 6, 30)),
    "2026": (date(2025, 1, 1), date(2026, 6, 30), date(2026, 6, 1), date(2026, 6, 30)),
}

ADOPTED_STATUSES = {"adopted", "passed", "approved"}

COMMENDATION = re.compile(
    r"commend|honoring|recognizing|celebrat|congratulat|memory of|"
    r"proclaim.*day|appreciation|retirement of",
    re.IGNORECASE,
)

SCRUB = [
    (re.compile(r"\[.*?\]", re.S), ""),
    (re.compile(r"FILE\s*NO\.?\s*\d{6}", re.I), ""),
    (re.compile(r"RESOLUTION\s*NO\.?\s*[\d-]+", re.I), ""),
    (re.compile(r"\b\d{1,2}\s*/\s*\d{1,2}\s*/\s*\d{2,4}\b"), ""),
    (
        re.compile(
            r"\b(January|February|March|April|May|June|July|August|September|"
            r"October|November|December)\s+\d{1,2},?\s+\d{4}\b"
        ),
        "",
    ),
    (re.compile(r"AMENDED IN (COMMITTEE|BOARD)", re.I), ""),
    (re.compile(r"[ \t]{2,}"), " "),
    (re.compile(r"\n{3,}"), "\n\n"),
]


def pick_leg_attachment(page):
    """From a LegislationDetail page, choose the best legislation-text PDF:
    'Leg Final' > highest 'Leg VerN' > None."""
    links = []
    for a in page.xpath("//a[contains(@href, 'View.ashx')]"):
        label = (a.text_content() or "").strip()
        if re.match(r"^Leg\b", label, re.I):
            links.append((label, a.get("href")))
    if not links:
        return None, None
    for label, href in links:
        if "final" in label.lower():
            return label, href

    def ver(label):
        m = re.search(r"(\d+)", label)
        return int(m.group(1)) if m else 0

    return max(links, key=lambda t: ver(t[0]))


def pdf_to_text(pdf_bytes):
    text_parts = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text_parts.append(page.extract_text() or "")
    return "\n".join(text_parts)


def clean_pdf_text(text):
    """SF legislation PDFs: numbered pleading lines (1-25 per page),
    repeated sponsor/page footers, and a trailing 'Tails' action summary."""
    # Cut the 'Tails' section (dates, votes, names -- all era giveaways)
    m = re.search(r"City and County of San Francisco\s*\n?\s*Tails", text)
    if m:
        text = text[: m.start()]

    lines = text.splitlines()

    # Drop leading pleading-paper line numbers ("9 WHEREAS, ..." -> "WHEREAS, ...")
    cleaned = []
    for line in lines:
        line = re.sub(r"^\s*\d{1,2}\s(?=\S)", "", line)
        if re.fullmatch(r"\s*\d{1,2}\s*", line):  # bare number lines
            continue
        cleaned.append(line.strip())

    # Drop any short line that repeats (footers: sponsor lists, page marks)
    counts = Counter(l for l in cleaned if l)
    cleaned = [l for l in cleaned if not (counts[l] >= 2 and len(l) < 80)]
    # Footer fragments (case-SENSITIVE: body prose says "Board of Supervisors")
    cleaned = [
        l
        for l in cleaned
        if not re.match(r"^(BOARD OF SUPERVISORS\b|Page \d+|Printed at)", l)
    ]
    # Lines that are only digit/date debris after number-stripping (e.g. "/7/18")
    cleaned = [l for l in cleaned if not re.fullmatch(r"[\d\s/.:-]*", l)]

    text = "\n".join(cleaned)
    for pat, repl in SCRUB:
        text = pat.sub(repl, text)
    return text.strip()


def norm_keys(row):
    return {k.replace("\xa0", " ").strip(): v for k, v in row.items()}


def cell(row, *names):
    for n in names:
        if n in row:
            v = row[n]
            if isinstance(v, dict):
                return v.get("label", "")
            return v or ""
    return ""


def parse_date(s):
    s = (s or "").strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def main():
    scraper = SFBillScraper(requests_per_minute=30, retry_attempts=3)
    out = Path("data")
    summary = []

    for era, (created_after, created_before, win_start, win_end) in WINDOWS.items():
        print(
            f"\n=== {era}: search created {created_after}..{created_before}, "
            f"keep Final Action {win_start}..{win_end} ==="
        )
        txt_dir = out / "txt" / era
        txt_dir.mkdir(parents=True, exist_ok=True)

        kept = 0
        with (out / f"resolutions_{era}.jsonl").open("w") as f:
            for raw_row in scraper.legislation(
                created_after=created_after, created_before=created_before
            ):
                row = norm_keys(raw_row)
                file_no = cell(row, "File #", "File#", "File Number")
                rtype = cell(row, "Type")
                status = cell(row, "Status")
                final_action = parse_date(
                    cell(row, "Final Action", "Final Action Date")
                )
                title = cell(row, "Title")

                if rtype != "Resolution":
                    continue
                if status.lower() not in ADOPTED_STATUSES:
                    continue
                if not final_action or not (win_start <= final_action <= win_end):
                    continue
                if COMMENDATION.search(title):
                    summary.append([era, file_no, "skipped_commendation", title[:80]])
                    print(f"  skip (commendation): {file_no}")
                    continue

                detail_url = row.get("url")
                detail_page = scraper.lxmlize(detail_url)
                label, pdf_url = pick_leg_attachment(detail_page)
                if not pdf_url:
                    summary.append([era, file_no, "skipped_no_leg_pdf", title[:80]])
                    print(f"  skip (no Leg attachment): {file_no}")
                    continue

                time.sleep(0.5)
                resp = scraper.get(pdf_url, verify=False)
                try:
                    raw_text = pdf_to_text(resp.content)
                except Exception as e:
                    summary.append([era, file_no, f"skipped_pdf_error", title[:80]])
                    print(f"  skip (pdf error {e}): {file_no}")
                    continue

                clean = clean_pdf_text(raw_text)
                if len(clean) < 400:
                    summary.append([era, file_no, "skipped_short_text", title[:80]])
                    print(f"  skip (short after clean): {file_no}")
                    continue

                f.write(
                    json.dumps(
                        {
                            "era": era,
                            "file_no": file_no,
                            "status": status,
                            "final_action": str(final_action),
                            "title": title,
                            "detail_url": detail_url,
                            "pdf_label": label,
                            "pdf_url": pdf_url,
                            "raw_text": raw_text,
                            "clean_text": clean,
                        }
                    )
                    + "\n"
                )
                safe = re.sub(r"[^\w-]", "_", file_no or "unknown")
                (txt_dir / f"{safe}.txt").write_text(clean)
                summary.append([era, file_no, "kept", title[:80]])
                kept += 1
                print(f"  kept: {file_no}  ({label})  {title[:50]}")

        print(f"  {era}: kept {kept}")

    out.mkdir(exist_ok=True)
    with (out / "summary.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["era", "file_no", "status", "title"])
        w.writerows(summary)

    print("\nDone -> data/summary.csv. Eyeball a few txt files per era:")
    print("older scans are OCR'd and may have artifacts ('1 O' for '10');")
    print("check that dates and sponsor names got scrubbed before the eval.")


if __name__ == "__main__":
    main()
