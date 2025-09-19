"""
MergeMate Minimal API
- FastAPI server with a single core endpoint: POST /v1/review
- Fetches a repository over HTTPS (read-only) at a given ref (branch/tag/commit)
- Optionally diffs two refs OR scans for keyword-based relevance
- Returns a compact JSON payload with relevant files & code snippets

Deliberately simple:
- No Slack, no GitLab/GitHub app webhooks, no admin surface
- No background workers; uses per-request temporary workspace
- Uses the `git` CLI via subprocess (avoids heavy deps)

Run:
  uvicorn app:app --reload --port 8000

Example curl (keywords):
  curl -sS -X POST http://localhost:8000/v1/review \
    -H 'Content-Type: application/json' \
    -d '{
      "repo_url": "https://github.com/pallets/flask.git",
      "ref": "main",
      "keywords": ["blueprint", "route"],
      "max_files": 5,
      "snippet_radius": 5
    }' | jq

Example curl (diff):
  curl -sS -X POST http://localhost:8000/v1/review \
    -H 'Content-Type: application/json' \
    -d '{
      "repo_url": "https://github.com/pallets/flask.git",
      "ref": "main",
      "base_ref": "HEAD~1",
      "max_files": 10
    }' | jq
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, HttpUrl, Field, validator

# ---------------------------
# Utilities
# ---------------------------

GIT_TIMEOUT = int(os.getenv("GIT_TIMEOUT_SECONDS", "60"))
MAX_REPO_SIZE_MB = int(os.getenv("MAX_REPO_SIZE_MB", "300"))
DEFAULT_INCLUDE_EXTS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rb", ".rs", ".cpp", ".cc", ".c", ".h", ".hpp",
    ".cs", ".kt", ".swift", ".php", ".scala", ".m", ".mm", ".sh", ".bash", ".ps1", ".yml", ".yaml", ".json", ".toml",
    ".gradle", ".md"
}

BINARY_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".gz", ".bz2", ".7z", ".tar", ".woff", ".woff2", ".ttf"}

SKIP_DIRS = {".git", "node_modules", "dist", "build", ".venv", "venv", ".tox", ".idea", ".vscode", "target", "out"}

SAFE_HOSTS_PATTERN = re.compile(r"^(?:https://)([^/]+)")


def run(cmd: List[str], cwd: Optional[Path] = None, timeout: int = GIT_TIMEOUT) -> Tuple[int, str, str]:
    """Run a subprocess command and capture output."""
    p = subprocess.Popen(cmd, cwd=str(cwd) if cwd else None, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        raise HTTPException(504, detail=f"Command timed out: {' '.join(cmd)}")
    return p.returncode, out, err


def validate_https_repo(url: str) -> None:
    m = SAFE_HOSTS_PATTERN.match(url)
    if not m:
        raise HTTPException(400, detail="repo_url must be HTTPS (e.g., https://host/org/repo.git)")
    # Optionally restrict to trusted hosts; left permissive by default.


def du_mb(path: Path) -> float:
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except FileNotFoundError:
                pass
    return total / (1024 * 1024)


# ---------------------------
# Relevance Finder
# ---------------------------

@dataclass
class Snippet:
    path: str
    start_line: int
    end_line: int
    preview: str


@dataclass
class RelevantFile:
    path: str
    score: float
    lines: int
    snippets: List[Snippet]


def is_text_code_file(p: Path) -> bool:
    if p.suffix.lower() in BINARY_EXTS:
        return False
    if p.suffix and p.suffix.lower() not in DEFAULT_INCLUDE_EXTS:
        # Allow config/docs too, but skip obviously huge or unknown binaries later by size
        return True  # be permissive; we’ll size-check below
    return True


def read_lines_safe(p: Path, max_bytes: int = 1024 * 1024) -> List[str]:
    # Don't read very large files into memory
    try:
        if p.stat().st_size > max_bytes:
            return []
        with p.open("r", encoding="utf-8", errors="ignore") as f:
            return f.read().splitlines()
    except Exception:
        return []


def keyword_score(lines: List[str], keywords: List[str]) -> Tuple[float, List[Snippet]]:
    if not lines or not keywords:
        return 0.0, []
    kw_re = re.compile("|".join(re.escape(k) for k in keywords), re.IGNORECASE)
    matches: List[int] = []
    for i, line in enumerate(lines, start=1):
        if kw_re.search(line):
            matches.append(i)
    score = float(len(matches))
    return score, [Snippet(path="", start_line=max(1, i - 5), end_line=min(len(lines), i + 5), preview="\n".join(lines[max(1, i - 5)-1:min(len(lines), i + 5)])) for i in matches[:10]]


def diff_changed_files(repo_dir: Path, base_ref: str, ref: str) -> List[str]:
    code, out, err = run(["git", "diff", "--name-only", f"{base_ref}..{ref}"], cwd=repo_dir)
    if code != 0:
        raise HTTPException(400, detail=f"git diff failed: {err.strip()}")
    return [p for p in out.splitlines() if p.strip()]


def collect_relevant(repo_dir: Path, *, keywords: Optional[List[str]] = None, changed_only: Optional[List[str]] = None, max_files: int = 20, snippet_radius: int = 5) -> List[RelevantFile]:
    results: List[RelevantFile] = []
    candidates: List[Path] = []

    if changed_only:
        candidates = [repo_dir / p for p in changed_only]
    else:
        for p in repo_dir.rglob("*"):
            if p.is_dir():
                if p.name in SKIP_DIRS:
                    # prune recursively
                    continue
                else:
                    continue  # rglob handles recursion
            if not is_text_code_file(p):
                continue
            candidates.append(p)

    for p in candidates:
        rel = p.relative_to(repo_dir).as_posix()
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        lines = read_lines_safe(p)
        if not lines:
            continue
        score = 1.0  # baseline for existence
        snippets: List[Snippet] = []
        if keywords:
            score, snippets = keyword_score(lines, keywords)
            for s in snippets:
                s.path = rel
        # Prefer code-like files even without keywords by giving extension bonus
        if not keywords and p.suffix.lower() in DEFAULT_INCLUDE_EXTS:
            score += 0.5
        if score > 0:
            results.append(RelevantFile(path=rel, score=score, lines=len(lines), snippets=snippets))

    # Sort by score desc, tie-breaker by shorter files first
    results.sort(key=lambda r: (-r.score, r.lines, r.path))
    return results[:max_files]


# ---------------------------
# Git operations
# ---------------------------

def shallow_clone(repo_url: str, ref: str, workdir: Path) -> Path:
    validate_https_repo(repo_url)
    # Clone with minimal history; fetch specific ref
    repo_dir = workdir / "repo"
    code, out, err = run(["git", "init"], cwd=workdir)
    if code != 0:
        raise HTTPException(500, detail=f"git init failed: {err.strip()}")
    code, out, err = run(["git", "remote", "add", "origin", repo_url], cwd=workdir)
    if code != 0:
        raise HTTPException(400, detail=f"Invalid repo_url or permissions: {err.strip()}")
    # Use partial clone to reduce bandwidth; fall back gracefully
    code, out, err = run(["git", "fetch", "--depth", "1", "--no-tags", "origin", ref], cwd=workdir)
    if code != 0:
        # Try fetching by full name refs/heads/<ref>
        alt = f"refs/heads/{ref}"
        code2, out2, err2 = run(["git", "fetch", "--depth", "1", "--no-tags", "origin", alt], cwd=workdir)
        if code2 != 0:
            raise HTTPException(400, detail=f"Cannot fetch ref '{ref}': {err2.strip() or err.strip()}")
        ref = alt
    code, out, err = run(["git", "checkout", "FETCH_HEAD"], cwd=workdir)
    if code != 0:
        raise HTTPException(500, detail=f"git checkout failed: {err.strip()}")
    # Sanity check size
    size_mb = du_mb(workdir)
    if size_mb > MAX_REPO_SIZE_MB:
        raise HTTPException(413, detail=f"Repository too large: {size_mb:.1f} MB (limit {MAX_REPO_SIZE_MB} MB)")
    return workdir


# ---------------------------
# API Models
# ---------------------------

class TargetRef(BaseModel):
    ref: str = Field(..., description="Branch, tag, or commit SHA to analyze")
    base_ref: Optional[str] = Field(None, description="Optional base for diff mode (e.g., main or SHA)")


class ReviewRequest(BaseModel):
    repo_url: HttpUrl = Field(..., description="HTTPS git repo URL")
    ref: str = Field(..., description="Branch, tag, or commit SHA")
    base_ref: Optional[str] = Field(None, description="Optional base ref for diff mode")
    keywords: Optional[List[str]] = Field(None, description="Keywords to find relevant code")
    max_files: int = Field(20, ge=1, le=100)
    snippet_radius: int = Field(5, ge=1, le=50)

    @validator("repo_url")
    def only_https(cls, v: HttpUrl) -> HttpUrl:
        if v.scheme != "https":
            raise ValueError("repo_url must use https://")
        return v


class SnippetOut(BaseModel):
    path: str
    start_line: int
    end_line: int
    preview: str


class RelevantFileOut(BaseModel):
    path: str
    score: float
    lines: int
    snippets: List[SnippetOut]


class ReviewResponse(BaseModel):
    repo_url: str
    ref: str
    base_ref: Optional[str]
    mode: str
    changed_files: Optional[List[str]]
    relevant: List[RelevantFileOut]


# ---------------------------
# FastAPI app
# ---------------------------

app = FastAPI(title="MergeMate Minimal API", version="0.1.0")


@app.get("/healthz")
def healthz() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/review", response_model=ReviewResponse)
def review(req: ReviewRequest) -> Any:
    # Workspace per request
    with tempfile.TemporaryDirectory(prefix="mergemate_") as tmp:
        workdir = Path(tmp)
        repo_dir = shallow_clone(str(req.repo_url), req.ref, workdir)

        changed_files: Optional[List[str]] = None
        mode = "keywords"
        if req.base_ref:
            # Find changed files between base_ref and ref
            try:
                # Fetch base if needed
                code, out, err = run(["git", "fetch", "--depth", "1", "--no-tags", "origin", req.base_ref], cwd=repo_dir)
                # ignore non-zero here; base might already be present or ref name might be ambiguous
            except HTTPException:
                pass
            changed_files = diff_changed_files(repo_dir, req.base_ref, "HEAD")
            mode = "diff"

        relevant = collect_relevant(
            repo_dir,
            keywords=req.keywords,
            changed_only=changed_files,
            max_files=req.max_files,
            snippet_radius=req.snippet_radius,
        )

        return ReviewResponse(
            repo_url=str(req.repo_url),
            ref=req.ref,
            base_ref=req.base_ref,
            mode=mode,
            changed_files=changed_files,
            relevant=[RelevantFileOut(
                path=r.path, score=r.score, lines=r.lines,
                snippets=[SnippetOut(**s.__dict__) for s in r.snippets],
            ) for r in relevant]
        )


# Optional utility endpoint to fetch raw file content (capped) — useful for clients
class FileRequest(BaseModel):
    repo_url: HttpUrl
    ref: str
    path: str
    max_bytes: int = Field(200_000, ge=1, le=1_000_000)


@app.post("/v1/file")
def get_file(req: FileRequest) -> Dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="mergemate_") as tmp:
        workdir = Path(tmp)
        repo_dir = shallow_clone(str(req.repo_url), req.ref, workdir)
        target = (repo_dir / req.path).resolve()
        if not target.exists() or not target.is_file():
            raise HTTPException(404, detail="File not found at ref")
        if not str(target).startswith(str(repo_dir.resolve())):
            raise HTTPException(400, detail="Path traversal detected")
        if target.suffix.lower() in BINARY_EXTS:
            raise HTTPException(415, detail="Binary files are not supported")
        if target.stat().st_size > req.max_bytes:
            raise HTTPException(413, detail="File too large; raise max_bytes to fetch")
        content = target.read_text(encoding="utf-8", errors="ignore")
        return {"path": req.path, "bytes": len(content.encode("utf-8")), "content": content}


# Root help
@app.get("/")
def root() -> Dict[str, Any]:
    return {
        "name": "MergeMate Minimal API",
        "version": "0.1.0",
        "endpoints": {
            "GET /healthz": "Liveness probe",
            "POST /v1/review": "Analyze a repo by ref and optionally diff vs base_ref; return relevant files/snippets",
            "POST /v1/file": "Fetch a single file's content at a ref (text only)",
        },
        "notes": [
            "No Slack, no Git provider webhooks. HTTPS read-only clone per request.",
            "Use keywords or base_ref to focus relevance; results are capped and sorted.",
        ],
    }
