"""The HTML shells.
Each one is static — the page fetches everything it shows from the API
"""

import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from backend.core import config
from backend.data import projects


router = APIRouter()

BASE_DIR = str(config.BASE_DIR)


def page(name: str) -> FileResponse:
    # Without a Cache-Control header a browser invents its own freshness from
    # Last-Modified, and a reload after an edit can quietly serve yesterday's
    # markup against today's scripts. Revalidating costs one 304.
    return FileResponse(os.path.join(BASE_DIR, "static", name),
                        headers={"Cache-Control": "no-cache"})


@router.get("/")
async def index():
    return page("index.html")


@router.get("/projects")
async def projects_page():
    return page("projects.html")


@router.get("/projects/{pid}")
async def project_page(pid: str):
    """The project shell, 404 when there is no such project.
    Membership is not checked here: the page loads for anyone, and the API
    calls it makes are what refuse a stranger
    """
    if not await projects.get_project(pid):
        raise HTTPException(404, f"project {pid} not found")
    return page("project.html")
