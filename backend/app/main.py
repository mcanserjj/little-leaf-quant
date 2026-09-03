from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .ai import create_review, get_review, list_reviews, migrate_legacy_reviews, test_deepseek_connection
from .backtest import backtest_service
from .config import PROJECT_ROOT
from .data_updates import run_scheduled_news_update, set_news_auto_sync, start_update, test_hithink_connection, update_status
from .execution import execution_service
from .review_context import backtest_readiness
from .review_schedule import due_target, record as record_review_schedule, status as review_schedule_status
from .secrets import clear_deepseek_key, clear_hithink_key, deepseek_status, hithink_status, save_deepseek_key, save_hithink_key
from .selection import run_selection
from .storage import all_groups, data_coverage, now_iso, overview
from .strategies import all_strategy_versions, approve_strategy


async def scheduled_review() -> None:
    target = due_target(datetime.now())
    if not target or review_schedule_status().get("lastCompletedTarget") == target:
        return
    try:
        record_review_schedule("running", target)
        review = await create_review("scheduled_weekly", deep=True)
        if review.get("materializedCandidates"):
            backtest_service.start()
        record_review_schedule("completed", target, lastCompletedTarget=target, reviewId=review.get("id"), error=None)
    except Exception as exc:
        record_review_schedule("deferred", target, error=str(exc))
        return


@asynccontextmanager
async def lifespan(app: FastAPI):
    migrate_legacy_reviews()
    execution_service.initialize()
    scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(
        scheduled_review,
        "interval",
        minutes=5,
        id="weekly_ai_review",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.add_job(
        execution_service.tick,
        "interval",
        seconds=5,
        id="market_quote_and_execution_tick",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        run_scheduled_news_update,
        "interval",
        minutes=10,
        id="cninfo_news_incremental_sync",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    app.state.scheduler = scheduler
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="小树叶炒股模拟器", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:3021", "http://localhost:3021"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class AIKeyPayload(BaseModel):
    apiKey: str


class ReviewPayload(BaseModel):
    deep: bool = False


class SelectionPayload(BaseModel):
    asOf: str | None = None


class ExecutionModePayload(BaseModel):
    mode: str


class AutoSyncPayload(BaseModel):
    enabled: bool


@app.get("/api/health")
def health():
    return {"status": "ok", "time": now_iso(), "independent": True}


@app.get("/api/overview")
def get_overview():
    return {**overview(), "ai": deepseek_status(), "hithink": hithink_status(), "execution": execution_service.status()}


@app.get("/api/groups")
def get_groups():
    return all_groups()


@app.get("/api/execution/status")
def get_execution_status():
    return execution_service.status()


@app.put("/api/execution/mode")
def put_execution_mode(payload: ExecutionModePayload):
    try:
        return execution_service.set_mode(payload.mode)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/execution/refresh")
def post_execution_refresh():
    return execution_service.tick()


@app.get("/api/data/coverage")
def get_coverage():
    return data_coverage()


@app.get("/api/data/updates")
def get_data_updates():
    return update_status()


@app.post("/api/data/updates/{key}")
def post_data_update(key: str):
    try:
        return start_update(key)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.put("/api/data/news/auto-sync")
def put_news_auto_sync(payload: AutoSyncPayload):
    return set_news_auto_sync(payload.enabled)


@app.get("/api/backtest/readiness")
def get_backtest_readiness():
    return backtest_readiness()


@app.get("/api/backtest/status")
def get_backtest_status():
    return backtest_service.status()


@app.post("/api/backtest/run")
def post_backtest_run():
    try:
        return backtest_service.start()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/selection/run")
def post_selection(payload: SelectionPayload):
    try:
        return run_selection(payload.asOf)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/ai/status")
def get_ai_status():
    return deepseek_status()


@app.put("/api/ai/key")
def put_ai_key(payload: AIKeyPayload):
    try:
        save_deepseek_key(payload.apiKey)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return deepseek_status()


@app.post("/api/ai/test")
async def test_ai_key(payload: AIKeyPayload | None = None):
    try:
        return await test_deepseek_connection(payload.apiKey if payload else None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"DeepSeek连接失败：{exc}") from exc


@app.delete("/api/ai/key")
def delete_ai_key():
    return clear_deepseek_key()


@app.get("/api/hithink/status")
def get_hithink_status():
    return hithink_status()


@app.put("/api/hithink/key")
def put_hithink_key(payload: AIKeyPayload):
    try:
        save_hithink_key(payload.apiKey)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return hithink_status()


@app.post("/api/hithink/test")
def test_hithink_key(payload: AIKeyPayload | None = None):
    try:
        return test_hithink_connection(payload.apiKey if payload else None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"HiThink连接失败：{exc}") from exc


@app.delete("/api/hithink/key")
def delete_hithink_key():
    return clear_hithink_key()


@app.get("/api/strategies")
def get_strategies():
    return all_strategy_versions()


@app.post("/api/strategies/{group_id}/versions/{version}/approve")
def post_strategy_approval(group_id: str, version: int):
    try:
        return approve_strategy(group_id, version)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/reviews")
def get_reviews():
    return list_reviews()


@app.get("/api/reviews/schedule/status")
def get_review_schedule_status():
    return review_schedule_status()


@app.get("/api/reviews/{review_id}")
def get_review_document(review_id: str):
    review = get_review(review_id)
    if not review:
        raise HTTPException(status_code=404, detail="复盘文档不存在")
    return review


@app.get("/api/reviews/{review_id}/markdown", response_class=PlainTextResponse)
def get_review_markdown(review_id: str):
    review = get_review(review_id)
    if not review:
        raise HTTPException(status_code=404, detail="复盘文档不存在")
    return review["markdown"]


@app.post("/api/reviews")
async def post_review(payload: ReviewPayload):
    try:
        review = await create_review("manual", deep=payload.deep)
        if review.get("materializedCandidates"):
            try:
                backtest_service.start()
            except ValueError:
                pass
        return review
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"DeepSeek调用失败：{exc}") from exc


frontend_dist = PROJECT_ROOT / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
