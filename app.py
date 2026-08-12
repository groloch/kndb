import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.api import (learning_routes, llm_routes, notes_routes,
                         pages_routes, projects_routes, sources_routes,
                         transfer_routes)
from backend.content import fetchers
from backend.core import config, db, migrations
from backend.data import store, users
from backend.integrations import llm


BASE_DIR = os.path.dirname(os.path.abspath(__file__))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    store.ensure_dirs()
    await db.init_db()
    await migrations.run()
    await users.ensure()  # the default identity always exists
    yield
    await llm.aclose()
    await fetchers.aclose()
    await db.dispose()


app = FastAPI(title="KNDB", lifespan=lifespan)


@app.exception_handler(HTTPException)
async def _http_error_handler(_req, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"ok": False, "error": exc.detail},
    )

@app.exception_handler(Exception)
async def _unhandled_error_handler(_req, exc: Exception):
    return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})


app.include_router(llm_routes.router)
app.include_router(sources_routes.router)
app.include_router(learning_routes.router)
app.include_router(projects_routes.router)
app.include_router(notes_routes.router)
app.include_router(transfer_routes.router)
app.include_router(pages_routes.router)

app.mount(
    "/static",
    StaticFiles(directory=os.path.join(BASE_DIR, "static")),
    name="static",
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app:app",
        host=config.SERVER_HOST,
        port=config.SERVER_PORT,
        reload=config.DEBUG,
    )
