"""FastAPI app factory. Wires routes, templates, static, lifespan."""
from __future__ import annotations
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from config import INGEST_WORKER_COUNT, WATCH_DIR, WATCH_DEBOUNCE_SEC
from webui.workers.ingest_queue import IngestQueue
from webui.workers.folder_watcher import FolderWatcher

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("openbrain.app")

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def get_templates() -> Jinja2Templates:
    return TEMPLATES


def _pending_drafts_count() -> int:
    from db.connection import get_conn
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM drafts WHERE status='pending'")
                return cur.fetchone()[0] or 0
    except Exception:
        return 0


def render(request, template: str, ctx: dict):
    """TemplateResponse wrapper that injects sidebar-wide context."""
    ctx = {**ctx, "pending_drafts": _pending_drafts_count()}
    return TEMPLATES.TemplateResponse(request, template, ctx)


def create_app(*, start_workers: bool = True) -> FastAPI:
    queue = IngestQueue()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        watcher: FolderWatcher | None = None
        if start_workers:
            await queue.start(worker_count=INGEST_WORKER_COUNT)
            log.info("ingest queue started with %d workers", INGEST_WORKER_COUNT)
            if WATCH_DIR:
                watch_path = Path(WATCH_DIR)
                if watch_path.is_dir():
                    watcher = FolderWatcher(watch_path, queue,
                                            debounce_sec=WATCH_DEBOUNCE_SEC)
                    await watcher.start()
                else:
                    log.warning("VB_WATCH_DIR=%s is not a directory; watcher disabled",
                                WATCH_DIR)
        try:
            yield {"queue": queue}
        finally:
            if watcher:
                await watcher.stop()
            if start_workers:
                await queue.stop()

    app = FastAPI(title="OpenBrain", lifespan=lifespan)
    app.state.queue = queue
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    @app.get("/")
    async def root():
        return RedirectResponse(url="/inventory", status_code=307)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    from webui.routes import auth, upload, inventory, docgen, drafts, spaces, chat
    app.include_router(auth.router)
    app.include_router(upload.router)
    app.include_router(inventory.router)
    app.include_router(docgen.router)
    app.include_router(drafts.router)
    app.include_router(spaces.router)
    app.include_router(chat.router)

    return app


# Convenience for `uvicorn webui.app:app` — workers start via lifespan
app = create_app(start_workers=True)
