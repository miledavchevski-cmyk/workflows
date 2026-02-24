from pydantic import BaseModel
from typing import Optional


class ResearchRequest(BaseModel):
    game_name: str


class ContentBriefRequest(BaseModel):
    keyword: str


class ResearchResponse(BaseModel):
    job_id: str


class JobStatus(BaseModel):
    job_id: str
    status: str  # "queued" | "running" | "complete" | "error"
    progress: list[str] = []
    report_html: Optional[str] = None
    error: Optional[str] = None
