"""The HTML shells.
Each one is static — the page fetches everything it shows from the API
"""

import os
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
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


async def _has_session(token: str) -> bool:
    if not token:
        return False
    from backend.content import auth

    return await auth.resolve_session(token) is not None


async def _protected_page(request: Request, name: str):
    if config.AUTH_MODE == "password":
        token = request.cookies.get(config.AUTH_COOKIE_NAME, "")
        if not await _has_session(token):
            next_url = quote(request.url.path + ("?" + request.url.query
                                                 if request.url.query else ""), safe="")
            return RedirectResponse(f"/login?next={next_url}", status_code=303)
    return page(request, name)


@router.get("/login")
async def login_page(request: Request):
    return page(request, "login.html")


@router.get("/")
async def index(request: Request):
    if config.AUTH_MODE == "password":
        token = request.cookies.get(config.AUTH_COOKIE_NAME, "")
        if await _has_session(token):
            return page(request, "index.html")
        return page(request, "login.html")
    return page(request, "index.html")


@router.get("/projects")
async def projects_page(request: Request):
    return await _protected_page(request, "projects.html")


@router.get("/projects/{pid}")
async def project_page(request: Request, pid: str):
    """The project shell, 404 when there is no such project.
    Membership is not checked here; all data APIs enforce access.
    """
    if not await projects.get_project(pid):
        raise HTTPException(404, f"project {pid} not found")
    return await _protected_page(request, "project.html")


@router.get("/agent")
async def agent_page(request: Request):
    """The user's own workspace agent at the top level.
    The pane is the same templates/agent.html a project's Agent tab includes;
    the page script binds it to the caller's personal workspace
    """
    return await _protected_page(request, "agent.html")


@router.get("/learning")
async def learning_page(request: Request):
    """The Learning tab at the top level: replay quizzes over a selection of
    the caller's workspace sources, full page.
    The shell only — everything it shows comes from the API, scoped to the
    caller's personal workspace by the quiz routes themselves
    """
    return await _protected_page(request, "learning.html")
