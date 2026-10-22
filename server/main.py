from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from server.agents.router import RouterAgent, build_openrouter_client
from server.config import load_environment
from server.webapp.web import router as eval_router, api_router
from server.webapp.api import router as viito_api_router
from server.webapp.telegram_api import router as telegram_api_router
from server.webapp.onlyoffice import router as onlyoffice_router
from server.webapp.odoo_api import router as odoo_api_router
from server.webapp.vikunja_api import router as vikunja_api_router

load_environment()

app = FastAPI(title="Activity AI Engine", version="0.1.0")

# Allow the Vite dev server, the same-origin build, and the 127.0.0.1 alias
# of the production port to call the API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:8001",
        "http://127.0.0.1:8001",
    ],
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(eval_router)
app.include_router(api_router)
app.include_router(viito_api_router, prefix="/api")
app.include_router(telegram_api_router, prefix="/api")
app.include_router(onlyoffice_router)        # /onlyoffice/*, /docbuilder/*, /wopi/* (docserver callbacks)
app.include_router(onlyoffice_router, prefix="/api")  # /api/onlyoffice/*, /api/docbuilder/*, /api/wopi/* (frontend fetch)
app.include_router(odoo_api_router, prefix="/api")  # /api/odoo/status, /api/odoo/{model}/list, /api/odoo/{model}/{id}
app.include_router(vikunja_api_router, prefix="/api")  # /api/vikunja/status, /api/vikunja/projects, /api/vikunja/tasks...

# Serve the built Viito React webapp (server/webapp/frontend/dist) at "/",
# with SPA fallback for client-side routes. The catch-all below is registered
# at the END of this module (after every explicit /api, /eval and app route)
# so explicit routes always take precedence over the SPA fallback.
_FRONTEND_DIST = Path(__file__).resolve().parents[1] / "server" / "webapp" / "frontend" / "dist"
if _FRONTEND_DIST.is_dir():
    app.mount(
        "/assets",
        StaticFiles(directory=_FRONTEND_DIST / "assets"),
        name="viito-assets",
    )


def _viito_spa(full_path: str):
    candidate = (_FRONTEND_DIST / full_path).resolve()
    if candidate.is_file() and candidate.is_relative_to(_FRONTEND_DIST.resolve()):
        return FileResponse(candidate)
    headers = {"Cache-Control": "no-cache, no-store, must-revalidate"}
    return FileResponse(_FRONTEND_DIST / "index.html", headers=headers)


# Frontend page routes: paths that are BOTH React client routes and FastAPI
# API routes (e.g. /conversations is a page AND a GET endpoint). On a hard
# refresh the browser sends Accept: text/html, so we serve the SPA (index.html)
# for these paths and let the API handle JSON fetches (Accept: */* or
# application/json). This prevents the API returning raw JSON (white screen)
# when the user refreshes a page.
_SPA_PAGE_PREFIXES = (
    "/conversations",
    "/files",
    
    "/gmail",
    "/telegram",
    "/onlyoffice",
    "/odoo",
    "/vikunja",
    "/about",
    "/",
)


@app.middleware("http")
async def _viito_spa_for_navigation(request, call_next):
    if request.method != "GET":
        return await call_next(request)
    accept = (request.headers.get("accept") or "").lower()
    if "text/html" not in accept:
        return await call_next(request)
    path = request.url.path
    if path.startswith(("/api/", "/assets/", "/health")):
        return await call_next(request)
    if not path.startswith(_SPA_PAGE_PREFIXES):
        return await call_next(request)
    # Browser navigation to a frontend page -> serve the SPA entry point.
    return _viito_spa(path.lstrip("/"))



class RouteRequest(BaseModel):
    intent: str = Field(min_length=1)


class RouteResponse(BaseModel):
    intent: str
    route: str
    model_answer: str


router_agent = RouterAgent()


@lru_cache(maxsize=1)
def build_runtime_router_agent() -> RouterAgent:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY is required to use the routing API in OpenRouter mode. Set ROUTER_PROVIDER=local only if you want the fallback classifier."
        )

    return RouterAgent(
        model_client=build_openrouter_client(
            model=os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.1-8b-instruct"),
            api_key=api_key,
            temperature=float(os.environ.get("OPENROUTER_TEMPERATURE", "0.0")),
            top_p=float(os.environ.get("OPENROUTER_TOP_P", "1.0")),
            max_tokens=int(os.environ.get("OPENROUTER_MAX_TOKENS", "64")),
            frequency_penalty=float(os.environ.get("OPENROUTER_FREQUENCY_PENALTY", "0.0")),
            presence_penalty=float(os.environ.get("OPENROUTER_PRESENCE_PENALTY", "0.0")),
            seed=int(os.environ.get("OPENROUTER_SEED", "42")),
            site_url=os.environ.get("OPENROUTER_SITE_URL"),
            app_name=os.environ.get("OPENROUTER_APP_NAME", "activity-ai-engine"),
        )
    )


def get_router_agent() -> RouterAgent:
    provider = os.environ.get("ROUTER_PROVIDER", "openrouter").lower()
    if provider == "openrouter":
        return build_runtime_router_agent()
    if provider == "local":
        return router_agent
    if provider == "auto":
        return build_runtime_router_agent()
    raise ValueError("ROUTER_PROVIDER must be one of: openrouter, local, auto")


RouterAgentDep = Annotated[RouterAgent, Depends(get_router_agent)]


@app.get("/health")
def read_root() -> dict[str, str]:
    return {"status": "ok", "service": "routing-agent"}


@app.post("/route", response_model=RouteResponse)
def route_intent(payload: RouteRequest, agent: RouterAgentDep) -> RouteResponse:
    prediction = agent.predict(payload.intent)
    return RouteResponse(intent=prediction.intent, route=prediction.predicted_label, model_answer=prediction.raw_response)


# SPA fallback (registered last): any path not matched by an explicit route
# serves the Viito frontend build, enabling BrowserRouter deep links.
if _FRONTEND_DIST.is_dir():
    @app.get("/{full_path:path}", include_in_schema=False)
    def viito_spa(full_path: str):
        return _viito_spa(full_path)

