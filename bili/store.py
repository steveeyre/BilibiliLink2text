"""队列与结果的本地持久化。

重启后队列不丢：已完成的任务连同文稿、章节、总结一起恢复，
行内「打开文件」可直接重开上次导出的 .md。
"""
from __future__ import annotations

import json
import sys
from dataclasses import fields
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import data_dir
from .models import Job
from .pipeline import VideoResult

STORE_VERSION = 1

_JOB_FIELDS = ("url", "title", "status", "error", "retries", "export_path", "created")
_RESULT_FIELDS = tuple(f.name for f in fields(VideoResult))


def store_file() -> Path:
    return data_dir() / "queue.json"


def _result_to_dict(r: Optional[VideoResult]) -> Optional[dict]:
    if r is None:
        return None
    return {k: getattr(r, k) for k in _RESULT_FIELDS}


def _result_from_dict(d) -> Optional[VideoResult]:
    if not isinstance(d, dict) or not isinstance(d.get("info"), dict):
        return None
    try:
        return VideoResult(**{k: d[k] for k in _RESULT_FIELDS if k in d})
    except TypeError:
        return None


def _normalize_status(status: str) -> str:
    # 「处理中」是上次被强制中断的任务，回到排队态让用户可以一键续跑
    return "排队中" if status == "处理中" else status


def save_jobs(jobs: list[Job]) -> None:
    payload = {
        "version": STORE_VERSION,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "jobs": [
            {**{k: getattr(j, k) for k in _JOB_FIELDS}, "result": _result_to_dict(j.result)}
            for j in jobs
        ],
    }
    f = store_file()
    tmp = f.parent / (f.name + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(f)
    except OSError as e:
        print(f"[store] 队列保存失败: {e}", file=sys.stderr)


def load_jobs() -> list[Job]:
    f = store_file()
    if not f.exists():
        return []
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        entries = data["jobs"]
        if not isinstance(entries, list):
            raise ValueError("jobs 字段不是列表")
    except Exception as e:
        print(f"[store] 队列文件损坏，已忽略 ({e})", file=sys.stderr)
        try:
            f.replace(f.with_name(f.name + ".corrupt"))
        except OSError:
            pass
        return []

    jobs: list[Job] = []
    for d in entries:
        if not isinstance(d, dict) or not d.get("url"):
            continue
        try:
            retries = int(d.get("retries") or 0)
        except (TypeError, ValueError):
            retries = 0
        job = Job(
            url=str(d["url"]),
            title=str(d.get("title") or ""),
            status=_normalize_status(str(d.get("status") or "排队中")),
            error=str(d.get("error") or ""),
            retries=retries,
            export_path=str(d.get("export_path") or ""),
            created=str(d.get("created") or ""),
        )
        if not job.created:
            job.created = datetime.now().strftime("%H:%M:%S")
        job.result = _result_from_dict(d.get("result"))
        if job.status == "完成" and job.result is None:
            job.status = "已停止"   # 文稿没能一起恢复，不谎报完成
        jobs.append(job)
    return jobs
