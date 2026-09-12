"""The HTML shells.
Each one is static — the page fetches everything it shows from the API
"""

import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.templating import Jinja2Templates

from backend.core import config
from backend.data import projects


router = APIRouter()

BASE_DIR = str(config.BASE_DIR)
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "static"))


def page(request: Request, name: str):
    # Without a Cache-Control header a browser invents its own freshness from
    # Last-Modified, and a reload after an edit can quietly serve yesterday's
    # markup against today's scripts. Revalidating costs one 304.
    return templates.TemplateResponse(
        {"request": request},
        name,
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/")
async def index(request: Request):
    return page(request, "index.html")


@router.get("/projects")
async def projects_page(request: Request):
    return page(request, "projects.html")


@router.get("/projects/{pid}")
async def project_page(request: Request, pid: str):
    """The project shell, 404 when there is no such project.
    Membership is not checked here: the page loads for anyone, and the API
    calls it makes are what refuse a stranger
    """
    if not await projects.get_project(pid):
        raise HTTPException(404, f"project {pid} not found")
    return page(request, "project.html")


@router.get("/agent")
async def agent_page(request: Request):
    """The user's own workspace agent at the top level.
    The pane is the same templates/agent.html a project's Agent tab includes;
    the page script binds it to the caller's personal workspace
    """
    return page(request, "agent.html")


@router.get("/learning")
async def learning_page(request: Request):
    """The Learning tab at the top level: replay quizzes over a selection of
    the caller's workspace sources, full page.
    The shell only — everything it shows comes from the API, scoped to the
    caller's personal workspace by the quiz routes themselves
    """
    return page(request, "learning.html")
