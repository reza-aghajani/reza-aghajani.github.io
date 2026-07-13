#!/usr/bin/env python3
# requirements: feedparser requests anthropic
# Run: python scripts/weekly_digest.py
# Env: ANTHROPIC_API_KEY must be set

import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

import feedparser
import anthropic

FEEDS = [
    ("Import AI",             "https://jack-clark.net/feed/"),
    ("Last Week in AI",       "https://lastweekin.ai/feed"),
    ("Ahead of AI",           "https://magazine.sebastianraschka.com/feed"),
    ("Davis Summarizes Papers","https://dblalock.substack.com/feed"),
]

INDEX_HTML = os.path.join(os.path.dirname(__file__), "..", "reading", "index.html")
FOLDERS_MARKER  = "// WEEKLY_DIGEST_FOLDERS_MARKER"
PAPERS_MARKER   = "// WEEKLY_DIGEST_PAPERS_MARKER"
CUTOFF_DAYS     = 7

EXTRACT_PROMPT = """\
You are a research assistant. Below is a newsletter issue about AI/ML.
Extract every paper, benchmark, or dataset that is explicitly mentioned.
Respond with ONLY a JSON array (no markdown, no commentary):
[
  {{"title": "...", "url": "...", "description": "..."}}
]
Where:
- title: full paper title as written in the newsletter
- url: arXiv link if mentioned, otherwise the most direct link to the paper; omit if none found
- description: 1-2 sentences on what the paper does and why the newsletter flagged it

Skip: product announcements without a paper, job listings, opinion pieces, paywalled content with no paper link, and fiction sections.
If no papers are found, return an empty array [].

Newsletter content:
{text}"""


def api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        sys.exit("ERROR: ANTHROPIC_API_KEY environment variable is not set.")
    return key


def strip_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&#\d+;", " ", text)
    text = re.sub(r"\s{3,}", "\n\n", text)
    return text.strip()


def fetch_recent_entries(feed_url: str, cutoff: datetime) -> list[dict]:
    feed = feedparser.parse(feed_url)
    recent = []
    for entry in feed.entries:
        published = entry.get("published_parsed") or entry.get("updated_parsed")
        if published:
            pub_dt = datetime(*published[:6], tzinfo=timezone.utc)
            if pub_dt < cutoff:
                continue
        content = ""
        if hasattr(entry, "content"):
            content = " ".join(c.get("value", "") for c in entry.content)
        elif hasattr(entry, "summary"):
            content = entry.summary
        recent.append({
            "title":   entry.get("title", ""),
            "link":    entry.get("link", ""),
            "content": strip_html(content)[:12000],
        })
    return recent


def extract_papers(client: anthropic.Anthropic, source_name: str, entry: dict) -> list[dict]:
    text = f"Title: {entry['title']}\nLink: {entry['link']}\n\n{entry['content']}"
    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            messages=[{"role": "user", "content": EXTRACT_PROMPT.format(text=text)}],
        )
        raw = msg.content[0].text.strip()
        # Strip markdown fences if present
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        papers = json.loads(raw)
        if not isinstance(papers, list):
            raise ValueError("Response is not a list")
        return papers
    except Exception as e:
        print(f"  WARNING: could not parse response for '{entry['title']}': {e}", file=sys.stderr)
        return []


def js_escape(s: str) -> str:
    s = str(s) if s else ""
    s = s.replace("\\", "\\\\")
    s = s.replace("'", "\\'")
    s = s.replace("\n", " ").replace("\r", " ")
    return s


def build_paper_line(folder: str, source_name: str, paper: dict) -> str:
    title = js_escape(paper.get("title", ""))
    note  = js_escape(paper.get("description", ""))
    url   = paper.get("url", "")
    parts = [
        f"folder: '{js_escape(folder)}'",
        f"subsection: '{js_escape(source_name)}'",
        f"title: '{title}'",
    ]
    if url:
        parts.append(f"arxiv: '{js_escape(url)}'")
    if note:
        parts.append(f"note: '{note}'")
    parts.append("done: false")
    return "  { " + ", ".join(parts) + " },"


def main():
    key = api_key()
    client = anthropic.Anthropic(api_key=key)

    now    = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=CUTOFF_DAYS)
    week_label = f"Week of {now.strftime('%Y-%m-%d')}"

    print(f"Fetching newsletters for {week_label} (cutoff: {cutoff.date()})...")

    all_paper_lines: list[str] = []

    for source_name, feed_url in FEEDS:
        print(f"\n[{source_name}] {feed_url}")
        try:
            entries = fetch_recent_entries(feed_url, cutoff)
        except Exception as e:
            print(f"  WARNING: failed to fetch feed: {e}", file=sys.stderr)
            continue

        if not entries:
            print(f"  No entries in the last {CUTOFF_DAYS} days.")
            continue

        print(f"  {len(entries)} recent entries — extracting papers via Claude...")
        for entry in entries:
            papers = extract_papers(client, source_name, entry)
            print(f"  '{entry['title'][:60]}' → {len(papers)} papers")
            for p in papers:
                all_paper_lines.append(build_paper_line(week_label, source_name, p))

    if not all_paper_lines:
        print("\nNo papers found across all sources — skipping file update.")
        sys.exit(0)

    print(f"\nFound {len(all_paper_lines)} papers total. Updating index.html...")

    html_path = os.path.realpath(INDEX_HTML)
    with open(html_path, encoding="utf-8") as f:
        content = f.read()

    # Insert week label into SECTIONS folders array
    folder_entry = f"    '{week_label}',\n    "
    if week_label in content:
        print(f"  Week label '{week_label}' already present — skipping SECTIONS update.")
    else:
        content = content.replace(
            FOLDERS_MARKER,
            folder_entry + FOLDERS_MARKER,
        )

    # Insert paper entries after the PAPERS marker
    new_papers_block = "\n".join(all_paper_lines) + "\n  "
    content = content.replace(
        PAPERS_MARKER + "\n",
        PAPERS_MARKER + "\n  " + new_papers_block,
    )

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"Done — {len(all_paper_lines)} papers written for '{week_label}'.")


if __name__ == "__main__":
    main()
