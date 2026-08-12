import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from backend.core import config
from backend.data import projects


router = APIRouter()

BASE_DIR = str(config.BASE_DIR)


@router.get("/")
async def index():
    return FileResponse(os.path.join(BASE_DIR, "static", "index.html"))


@router.get("/projects")
async def projects_page():
    return FileResponse(os.path.join(BASE_DIR, "static", "projects.html"))


@router.get("/projects/{pid}")
async def project_page(pid: str):
    if not await projects.get_project(pid):
        raise HTTPException(404, f"project {pid} not found")
    return FileResponse(os.path.join(BASE_DIR, "static", "project.html"))
