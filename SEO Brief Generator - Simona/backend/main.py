import asyncio
import os
import sys
import uuid
import threading
from datetime import datetime
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse

load_dotenv()

# Ensure backend/ is on sys.path so crew imports work
sys.path.insert(0, os.path.dirname(__file__))

app = FastAPI(title="Casino SEO Research API", version="1.0.0")

# ── CORS ──────────────────────────────────────────────────────────────────────
# "null" (string) covers browsers opening seo-research.html via file:// protocol
_raw_origins = os.getenv(
    "CORS_ORIGINS",
    '["http://localhost:8000","http://127.0.0.1:8000","null"]',
)
try:
    import json
    _origins = json.loads(_raw_origins)
except Exception:
    _origins = ["http://localhost:8000", "http://127.0.0.1:8000", "null"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Job store ─────────────────────────────────────────────────────────────────
# { job_id: { status, progress: [str], report_html, error } }
job_store: dict[str, dict[str, Any]] = {}


# ── Background worker ─────────────────────────────────────────────────────────

def _push_progress(job_id: str, message: str) -> None:
    if job_id in job_store:
        job_store[job_id]["progress"].append(message)


def _make_step_callback(job_id: str):
    """Returns a step_callback compatible with CrewAI's agent step hook."""
    def callback(step_output):
        try:
            # step_output may be an AgentFinish, AgentAction, or str depending on version
            if hasattr(step_output, "log"):
                msg = step_output.log[:200]
            elif hasattr(step_output, "text"):
                msg = step_output.text[:200]
            else:
                msg = str(step_output)[:200]
            _push_progress(job_id, msg)
        except Exception:
            pass
    return callback


def _run_crew(job_id: str, game_name: str) -> None:
    """Runs the SEO crew in a background thread."""
    try:
        _push_progress(job_id, f"Starting research for: {game_name}")
        job_store[job_id]["status"] = "running"

        from crew.seo_crew import build_seo_crew

        step_cb = _make_step_callback(job_id)
        crew = build_seo_crew(game_name, step_callback=step_cb)

        _push_progress(job_id, "Crew assembled — running SEO Keyword Agent...")
        result = crew.kickoff()

        # Extract HTML from result
        if hasattr(result, "raw"):
            report_html = result.raw
        else:
            report_html = str(result)

        # Ensure it's actually HTML; if not, wrap it
        if not report_html.strip().startswith("<!"):
            report_html = _wrap_in_html(report_html, game_name)

        job_store[job_id]["report_html"] = report_html
        job_store[job_id]["status"] = "complete"
        _push_progress(job_id, "Research complete!")

    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        job_store[job_id]["status"] = "error"
        job_store[job_id]["error"] = str(exc)
        _push_progress(job_id, f"ERROR: {exc}")
        print(f"[crew error] {tb}", flush=True)


def _wrap_in_html(content: str, game_name: str) -> str:
    """Fallback: wrap plain text/markdown report in styled HTML."""
    import html as html_lib
    escaped = html_lib.escape(content).replace("\n", "<br>")
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>SEO Report: {html_lib.escape(game_name)}</title>
<style>
  body {{ background: #0f0f1a; color: #e2e8f0; font-family: sans-serif; padding: 2rem; }}
  h1 {{ color: #7c3aed; }}
  pre {{ white-space: pre-wrap; word-break: break-word; }}
  footer {{ margin-top: 2rem; color: #666; font-size: 0.8rem; }}
</style>
</head>
<body>
<h1>SEO Research Report: {html_lib.escape(game_name)}</h1>
<pre>{escaped}</pre>
<footer>Generated {ts}</footer>
</body>
</html>"""


# ── Endpoints ─────────────────────────────────────────────────────────────────

from schemas.models import ResearchRequest, ResearchResponse, JobStatus, ContentBriefRequest  # noqa: E402


@app.post("/api/research", response_model=ResearchResponse)
async def start_research(request: ResearchRequest, background_tasks: BackgroundTasks):
    game_name = request.game_name.strip()
    if not game_name:
        raise HTTPException(status_code=422, detail="game_name cannot be empty")

    job_id = str(uuid.uuid4())
    job_store[job_id] = {
        "status": "queued",
        "progress": [],
        "report_html": None,
        "error": None,
    }

    # Run crew in a real thread (crew.kickoff() is synchronous/blocking)
    thread = threading.Thread(target=_run_crew, args=(job_id, game_name), daemon=True)
    thread.start()

    return ResearchResponse(job_id=job_id)


@app.get("/api/research/{job_id}/stream")
async def stream_research(job_id: str):
    if job_id not in job_store:
        raise HTTPException(status_code=404, detail="Job not found")

    async def event_generator():
        last_idx = 0
        heartbeat_counter = 0

        while True:
            job = job_store.get(job_id)
            if job is None:
                yield {"event": "error", "data": "Job not found"}
                return

            # Send any new progress messages
            progress = job.get("progress", [])
            while last_idx < len(progress):
                yield {"event": "progress", "data": progress[last_idx]}
                last_idx += 1

            status = job.get("status")

            # Terminal state: complete or error
            if status == "complete":
                import json as _json
                payload = _json.dumps({
                    "report_html": job.get("report_html", ""),
                })
                yield {"event": "complete", "data": payload}
                return
            elif status == "error":
                yield {"event": "error", "data": job.get("error", "Unknown error")}
                return

            # Heartbeat every ~15 s to keep proxy/browser alive
            heartbeat_counter += 1
            if heartbeat_counter >= 15:
                yield {"event": "heartbeat", "data": "ping"}
                heartbeat_counter = 0

            await asyncio.sleep(1)

    return EventSourceResponse(event_generator())


@app.get("/api/research/{job_id}", response_model=JobStatus)
async def get_job_status(job_id: str):
    if job_id not in job_store:
        raise HTTPException(status_code=404, detail="Job not found")
    job = job_store[job_id]
    return JobStatus(
        job_id=job_id,
        status=job["status"],
        progress=job["progress"],
        report_html=job.get("report_html"),
        error=job.get("error"),
    )


@app.get("/health")
async def health():
    return {"status": "ok", "jobs": len(job_store)}


# ── Content Brief endpoints ────────────────────────────────────────────────────

# Reuse the same job_store — brief jobs are prefixed with "brief-" in their id.

@app.post("/api/content-brief", response_model=ResearchResponse)
async def start_content_brief(request: ContentBriefRequest):
    keyword = request.keyword.strip()
    if not keyword:
        raise HTTPException(status_code=422, detail="keyword cannot be empty")

    from brief_worker import run_content_brief

    job_id = "brief-" + str(uuid.uuid4())
    job_store[job_id] = {
        "status": "queued",
        "progress": [],
        "report_html": None,
        "error": None,
    }

    thread = threading.Thread(
        target=run_content_brief,
        args=(job_store, job_id, keyword, request.competitor_urls or None),
        daemon=True,
    )
    thread.start()

    return ResearchResponse(job_id=job_id)


@app.get("/api/content-brief/{job_id}/stream")
async def stream_content_brief(job_id: str):
    if job_id not in job_store:
        raise HTTPException(status_code=404, detail="Job not found")

    async def event_generator():
        last_idx = 0
        heartbeat_counter = 0

        while True:
            job = job_store.get(job_id)
            if job is None:
                yield {"event": "error", "data": "Job not found"}
                return

            progress = job.get("progress", [])
            while last_idx < len(progress):
                yield {"event": "progress", "data": progress[last_idx]}
                last_idx += 1

            status = job.get("status")

            if status == "complete":
                import json as _json
                payload = _json.dumps({"report_html": job.get("report_html", "")})
                yield {"event": "complete", "data": payload}
                return
            elif status == "error":
                yield {"event": "error", "data": job.get("error", "Unknown error")}
                return

            heartbeat_counter += 1
            if heartbeat_counter >= 15:
                yield {"event": "heartbeat", "data": "ping"}
                heartbeat_counter = 0

            await asyncio.sleep(1)

    return EventSourceResponse(event_generator())


@app.get("/api/content-brief/{job_id}", response_model=JobStatus)
async def get_content_brief_status(job_id: str):
    if job_id not in job_store:
        raise HTTPException(status_code=404, detail="Job not found")
    job = job_store[job_id]
    return JobStatus(
        job_id=job_id,
        status=job["status"],
        progress=job["progress"],
        report_html=job.get("report_html"),
        error=job.get("error"),
    )


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn

    host = os.getenv("BACKEND_HOST", "127.0.0.1")
    port = int(os.getenv("BACKEND_PORT", "8000"))
    uvicorn.run("main:app", host=host, port=port, reload=False)
