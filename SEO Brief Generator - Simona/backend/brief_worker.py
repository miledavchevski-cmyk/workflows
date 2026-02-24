"""
SEO Content Brief worker — replicates the N8N workflow:
  keyword → Serper search → fetch top 5 pages → BeautifulSoup extract
           → Claude analysis → formatted HTML report
"""
import html as html_lib
import os
from datetime import datetime, timezone
from typing import Any

import httpx
from bs4 import BeautifulSoup


# ── Progress helper ────────────────────────────────────────────────────────────

def _push(job_store: dict, job_id: str, msg: str) -> None:
    if job_id in job_store:
        job_store[job_id]["progress"].append(msg)


# ── Main worker (runs in a thread) ─────────────────────────────────────────────

def run_content_brief(job_store: dict[str, Any], job_id: str, keyword: str) -> None:
    try:
        job_store[job_id]["status"] = "running"
        _push(job_store, job_id, f"Starting content brief for: {keyword}")

        # ── Step 1: Serper search ──────────────────────────────────────────────
        serper_key = os.getenv("SERPER_API_KEY", "")
        if not serper_key:
            raise ValueError("SERPER_API_KEY is not set in environment")

        _push(job_store, job_id, "Searching Google via Serper API...")
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": serper_key, "Content-Type": "application/json"},
                json={"q": keyword},
            )
            resp.raise_for_status()
            search_data = resp.json()

        organic = search_data.get("organic", [])[:5]
        if not organic:
            raise ValueError("No organic search results returned by Serper")

        _push(job_store, job_id, f"Found {len(organic)} results — fetching competitor pages...")

        # ── Step 2: Fetch + extract each page ─────────────────────────────────
        competitor_data = []
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
        }

        for i, result in enumerate(organic, 1):
            url = result.get("link", "")
            fallback_title = result.get("title", f"Page {i}")
            _push(job_store, job_id, f"[{i}/{len(organic)}] Fetching {url[:70]}...")

            try:
                with httpx.Client(timeout=30, follow_redirects=True) as client:
                    page_resp = client.get(url, headers=headers)
                    page_resp.raise_for_status()
                    raw_html = page_resp.text

                soup = BeautifulSoup(raw_html, "lxml")

                # Remove noise
                for tag in soup(["script", "style", "nav", "footer", "header"]):
                    tag.decompose()

                # Main content selector (mirrors N8N HTML node)
                content_el = (
                    soup.find("article")
                    or soup.find(class_="post-content")
                    or soup.find(class_="entry-content")
                    or soup.find("main")
                    or soup.find("body")
                )
                content_text = (
                    content_el.get_text(separator=" ", strip=True)[:800]
                    if content_el else ""
                )

                h1 = soup.find("h1") or soup.find(class_="title") or soup.find(class_="post-title")
                page_title = h1.get_text(strip=True)[:120] if h1 else fallback_title

                competitor_data.append({
                    "position": i,
                    "url": url,
                    "title": page_title,
                    "content": content_text,
                })
                _push(job_store, job_id, f"Extracted content from page {i}: {page_title[:50]}")

            except Exception as exc:
                _push(job_store, job_id, f"Skipped page {i} ({exc})")
                competitor_data.append({
                    "position": i, "url": url, "title": fallback_title, "content": ""
                })

        # ── Step 3: Build AI prompt ────────────────────────────────────────────
        _push(job_store, job_id, "Building competitor analysis prompt...")

        comp_summary = "COMPETITOR ANALYSIS:\n\n"
        for item in competitor_data:
            if item["content"]:
                comp_summary += f"Page {item['position']}: {item['title']}\n"
                comp_summary += f"Content preview: {item['content']}...\n\n"

        prompt = (
            f'You are an expert SEO content strategist. Analyze the following competitor '
            f'content for the keyword "{keyword}" and create a detailed content brief.\n\n'
            f"{comp_summary}\n"
            "Based on this analysis, provide the following sections. "
            "Use proper Markdown formatting: ## for section headings, ### for sub-headings, "
            "and - for bullet points.\n\n"
            "## 1. Search Intent\n"
            "What are users actually looking for?\n\n"
            "## 2. Common Topics\n"
            "What do all top-ranking pages cover?\n\n"
            "## 3. Content Gaps\n"
            "What is missing from most articles?\n\n"
            "## 4. Recommended Structure\n"
            "List the headings and sections we should include.\n\n"
            "## 5. Word Count Target\n"
            "Recommended word count based on competitor analysis.\n\n"
            "## 6. Unique Angle\n"
            "How can we stand out from competitors?\n\n"
            "Be specific and actionable."
        )

        # ── Step 4: Claude analysis ────────────────────────────────────────────
        _push(job_store, job_id, "Sending to Claude for content brief analysis...")
        import anthropic

        ac = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        message = ac.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        brief_text = message.content[0].text
        _push(job_store, job_id, "Claude analysis complete — formatting report...")

        # ── Step 5: Format HTML report ─────────────────────────────────────────
        report_html = _format_report(keyword, brief_text, competitor_data)

        job_store[job_id]["report_html"] = report_html
        job_store[job_id]["status"] = "complete"
        _push(job_store, job_id, "Content brief ready!")

    except Exception as exc:
        import traceback
        print(f"[brief error] {traceback.format_exc()}", flush=True)
        job_store[job_id]["status"] = "error"
        job_store[job_id]["error"] = str(exc)
        _push(job_store, job_id, f"ERROR: {exc}")


# ── HTML formatter ─────────────────────────────────────────────────────────────

def _format_report(keyword: str, brief_text: str, competitors: list) -> str:
    import markdown as md_lib

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    brief_html = md_lib.markdown(brief_text, extensions=["extra", "nl2br"])

    comp_rows = "".join(
        f'<tr><td>{c["position"]}</td>'
        f'<td><a href="{html_lib.escape(c["url"])}" target="_blank">'
        f'{html_lib.escape(c["title"][:80])}</a></td></tr>\n'
        for c in competitors
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Content Brief: {html_lib.escape(keyword)}</title>
<style>
  body {{
    background: #f8fafc;
    color: #1e293b;
    font-family: 'Segoe UI', Arial, sans-serif;
    padding: 2rem;
    max-width: 860px;
    margin: 0 auto;
    line-height: 1.7;
  }}
  h1 {{ color: #7c3aed; margin-bottom: 0.25rem; font-size: 1.6rem; }}
  .meta {{ color: #64748b; font-size: 0.85rem; margin-bottom: 2rem; }}

  .brief-body {{ background: white; border-radius: 8px; padding: 2rem 2.5rem;
                 box-shadow: 0 1px 4px rgba(0,0,0,0.08); margin-bottom: 1.5rem; }}

  h2 {{
    color: #7c3aed;
    font-size: 1.1rem;
    font-weight: 700;
    margin: 2rem 0 0.5rem;
    padding-bottom: 0.3rem;
    border-bottom: 2px solid #ede9fe;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }}
  h2:first-child {{ margin-top: 0; }}

  h3 {{
    color: #4c1d95;
    font-size: 0.95rem;
    font-weight: 600;
    margin: 1rem 0 0.3rem;
  }}

  p {{ margin: 0.5rem 0; }}

  ul, ol {{
    padding-left: 1.5rem;
    margin: 0.4rem 0 0.8rem;
  }}
  li {{ margin: 0.3rem 0; }}

  strong {{ color: #0f172a; }}

  .competitors {{
    background: white;
    border-radius: 8px;
    padding: 1.5rem 2rem;
    box-shadow: 0 1px 4px rgba(0,0,0,0.08);
  }}
  .competitors h2 {{ margin-top: 0; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.88rem; }}
  th {{ text-align: left; padding: 0.5rem 0.75rem; background: #f1f5f9; color: #475569; }}
  td {{ padding: 0.5rem 0.75rem; border-top: 1px solid #e2e8f0; }}
  a {{ color: #7c3aed; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
<h1>Content Brief: {html_lib.escape(keyword)}</h1>
<div class="meta">
  Generated {ts} &nbsp;·&nbsp;
  {len(competitors)} competitors analyzed &nbsp;·&nbsp;
  Powered by Claude Sonnet 4.6
</div>

<div class="brief-body">
{brief_html}
</div>

<div class="competitors">
  <h2>Analyzed Competitors</h2>
  <table>
    <thead><tr><th>#</th><th>Page</th></tr></thead>
    <tbody>{comp_rows}</tbody>
  </table>
</div>
</body>
</html>"""
