#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Judge clients: an OpenRouter-backed judge and a Mock judge for offline tests.

Design:
  - A judge is any object with .score(article_id, content) -> JudgeVote for a
    FIXED model. The runner fans out over judges × articles.
  - Outputs are cached on disk keyed by (model, prompt_version, article_id) so
    re-runs, added judges, or resumed runs never re-pay for a completed cell.
  - Robust JSON parsing (strips code fences, extracts the first {...}); a bad
    response yields JudgeVote(ok=False, error=...) rather than crashing the run.
"""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import os
import re
import time
from typing import Callable, Optional

import requests

from rubric import (PROMPT_VERSION, SYSTEM_PROMPT, build_user_prompt,
                    GENRE_LABELS)
from aggregate import JudgeVote

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Candidate judge pool (the "5 potential judges"). VERIFY these ids against the
# live OpenRouter catalog before running — model slugs change over time. Pick a
# diverse set (different labs) so ensemble agreement is not shared-bias.
DEFAULT_JUDGE_MODELS = [
    "openai/gpt-oss-120b",
    "google/gemini-3-flash-preview",
    "meta-llama/llama-3.3-70b-instruct",
    "qwen/qwen3.5-flash-02-23",
]

MAX_RETRIES = 4
BACKOFF_BASE = 2.0
TIMEOUT = 60
MAX_CONCURRENCY = 6


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_judge_json(raw: str, model: str) -> JudgeVote:
    """Parse a model's raw text into a JudgeVote, tolerant of code fences and
    surrounding prose. Validates the facet ranges."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    m = _JSON_RE.search(text)
    if not m:
        return JudgeVote(model=model, genre="other", stance_presence=0,
                         discourse=0, ok=False,
                         error=f"no json in response: {raw[:120]!r}")
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        return JudgeVote(model=model, genre="other", stance_presence=0,
                         discourse=0, ok=False, error=f"json decode: {e}")

    genre = str(obj.get("genre", "")).strip()
    try:
        stance = int(obj.get("stance_presence"))
        disc = int(obj.get("discourse"))
    except (TypeError, ValueError):
        return JudgeVote(model=model, genre="other", stance_presence=0,
                         discourse=0, ok=False,
                         error=f"non-int facet: {obj!r}")

    if genre not in GENRE_LABELS or not (0 <= stance <= 3 and 0 <= disc <= 3):
        return JudgeVote(model=model, genre="other", stance_presence=0,
                         discourse=0, ok=False,
                         error=f"out-of-range: {obj!r}")

    return JudgeVote(model=model, genre=genre, stance_presence=stance,
                     discourse=disc, rationale=str(obj.get("rationale", ""))[:300],
                     ok=True)


# --------------------------------------------------------------------------- #
# Disk cache
# --------------------------------------------------------------------------- #
class Cache:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _path(self, model: str, article_id: str) -> str:
        key = hashlib.sha1(
            f"{PROMPT_VERSION}|{model}|{article_id}".encode()).hexdigest()
        return os.path.join(self.root, f"{key}.json")

    def get(self, model: str, article_id: str) -> Optional[JudgeVote]:
        p = self._path(model, article_id)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                d = json.load(fh)
            return JudgeVote(**d)
        return None

    def put(self, model: str, article_id: str, vote: JudgeVote) -> None:
        p = self._path(model, article_id)
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(vote.__dict__, fh, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Judges
# --------------------------------------------------------------------------- #
class OpenRouterJudge:
    def __init__(self, model: str, api_key: Optional[str] = None,
                 temperature: float = 0.0):
        self.model = model
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY not set.")
        self.temperature = temperature

    def score(self, article_id: str, content: str) -> JudgeVote:
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(content)},
            ],
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
        last = ""
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                r = requests.post(OPENROUTER_URL, json=payload, headers=headers,
                                  timeout=TIMEOUT)
                if r.status_code == 400 and "response_format" in r.text:
                    payload.pop("response_format", None)   # model lacks JSON mode
                    raise _Retry("dropping response_format")
                if r.status_code in (408, 429, 500, 502, 503, 504):
                    raise _Retry(f"HTTP {r.status_code}")
                r.raise_for_status()
                raw = r.json()["choices"][0]["message"]["content"]
                return parse_judge_json(raw, self.model)
            except _Retry as e:
                last = str(e)
            except (requests.RequestException, KeyError, ValueError) as e:
                last = f"{type(e).__name__}: {e}"
            if attempt < MAX_RETRIES:
                time.sleep(BACKOFF_BASE ** attempt)
        return JudgeVote(model=self.model, genre="other", stance_presence=0,
                         discourse=0, ok=False, error=f"failed: {last}")


class MockJudge:
    """Deterministic offline judge for testing the pipeline without an API.
    Accepts a scoring function fn(model, article_id, content) -> dict."""
    def __init__(self, model: str, fn: Callable[[str, str, str], dict]):
        self.model = model
        self.fn = fn

    def score(self, article_id: str, content: str) -> JudgeVote:
        return parse_judge_json(json.dumps(self.fn(self.model, article_id, content)),
                                self.model)


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def run_panel(articles: list[dict], judges: list, cache: Optional[Cache] = None,
              content_key: str = "content", id_key: str = "article_id",
              max_concurrency: int = MAX_CONCURRENCY) -> dict[str, list[JudgeVote]]:
    """Score every article with every judge. Returns {article_id: [JudgeVote,...]}.
    Uses the cache to skip already-scored (model, article) cells."""
    results: dict[str, list[JudgeVote]] = {a[id_key]: [] for a in articles}
    tasks = []
    for a in articles:
        for j in judges:
            tasks.append((a, j))

    def _one(a, j):
        aid, content = a[id_key], a[content_key]
        if cache is not None:
            hit = cache.get(j.model, aid)
            if hit is not None:
                return aid, hit
        vote = j.score(aid, content)
        if cache is not None and vote.ok:
            cache.put(j.model, aid, vote)
        return aid, vote

    with cf.ThreadPoolExecutor(max_workers=max_concurrency) as ex:
        futs = [ex.submit(_one, a, j) for a, j in tasks]
        for fut in cf.as_completed(futs):
            aid, vote = fut.result()
            results[aid].append(vote)
    return results


class _Retry(Exception):
    pass
