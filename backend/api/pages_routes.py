import os

from fastapi import APIRouter
from fastapi.responses import FileResponse

from backend.core import config


router = APIRouter()

BASE_DIR = str(config.BASE_DIR)


@router.get("/")
async def index():
    return FileResponse(os.path.join(BASE_DIR, "static", "index.html"))


@router.get("/projects")
async def projects_page():
    return FileResponse(os.path.join(BASE_DIR, "static", "projects.html"))
