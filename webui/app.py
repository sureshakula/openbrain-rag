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
        import settings.core as sc
        from db.connection import get_conn
        watcher: FolderWatcher | None = None
        if start_workers:
            try:
                with get_conn() as conn:
                    worker_count = sc.get_int(conn, "ingest_worker_count")
                    watch_enabled = sc.get_bool(conn, "watch_enabled")
                    watch_subfolder = sc.get(conn, "watch_subfolder")
                    debounce = sc.get_int(conn, "watch_debounce_sec")
            except Exception:
                worker_count = INGEST_WORKER_COUNT
                watch_enabled = bool(WATCH_DIR)
                watch_subfolder = ""
                debounce = WATCH_DEBOUNCE_SEC
            await queue.start(worker_count=worker_count)
            log.info("ingest queue started with %d workers", worker_count)
            if watch_enabled and WATCH_DIR:
                watch_path = Path(WATCH_DIR) / watch_subfolder if watch_subfolder else Path(WATCH_DIR)
                if watch_path.is_dir():
                    watcher = FolderWatcher(watch_path, queue, debounce_sec=debounce)
                    await watcher.start()
                else:
                    log.warning("watch path %s is not a directory; watcher disabled", watch_path)
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

    from webui.routes import (auth, upload, inventory, docgen, drafts,
                              spaces, chat, settings as settings_routes)
    app.include_router(auth.router)
    app.include_router(upload.router)
    app.include_router(inventory.router)
    app.include_router(docgen.router)
    app.include_router(drafts.router)
    app.include_router(spaces.router)
    app.include_router(chat.router)
    app.include_router(settings_routes.router)

    return app


# Convenience for `uvicorn webui.app:app` — workers start via lifespan
app = create_app(start_workers=True)
