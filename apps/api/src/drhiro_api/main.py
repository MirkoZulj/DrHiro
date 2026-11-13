"""drHiro Core API application entry point."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from drhiro_api.config import get_settings
from drhiro_api.routers import auth, auth_web, dashboard, datapoints, import_csv, ingest, me, meals, medical, openclaw_tools, privacy, reminders
from drhiro_api.routers import analytics, medications, tools_analytics, tools_meds
from drhiro_api.routers import files, health_card
from drhiro_api.routers import backups, exports_v2, nutrition_coverage, intake, system_food_micros
from drhiro_api.routers.questionnarium import router as questionnarium_router
from drhiro_api.routers.xiaomi_csv import router as xiaomi_csv_router

settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version=settings.version,
    description="drHiro Core API - canonical health-data platform. Multi-user, consent-scoped, audit-logged.",
    docs_url="/docs",
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.miniapp_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/health")
def health():
    return {"status": "ok", "app": settings.app_name, "version": settings.version}


app.include_router(auth.router, prefix="/api/v1")
app.include_router(auth_web.router, prefix="/api/v1")
app.include_router(import_csv.router, prefix="/api/v1")
app.include_router(xiaomi_csv_router, prefix="/api/v1")
app.include_router(ingest.router, prefix="/api/v1")
app.include_router(meals.router, prefix="/api/v1")
app.include_router(medical.router, prefix="/api/v1")
app.include_router(dashboard.router, prefix="/api/v1")
app.include_router(reminders.router, prefix="/api/v1")
app.include_router(privacy.router, prefix="/api/v1")
app.include_router(datapoints.router, prefix="/api/v1")
app.include_router(openclaw_tools.router, prefix="/api/v1")
app.include_router(tools_meds.router, prefix="/api/v1")
app.include_router(tools_analytics.router, prefix="/api/v1")
app.include_router(medications.router, prefix="/api/v1")
app.include_router(analytics.router, prefix="/api/v1")
app.include_router(health_card.router, prefix="/api/v1")
app.include_router(files.router, prefix="/api/v1")
app.include_router(exports_v2.router, prefix="/api/v1")
app.include_router(nutrition_coverage.router, prefix="/api/v1")
app.include_router(intake.router, prefix="/api/v1")
app.include_router(system_food_micros.router, prefix="/api/v1")
app.include_router(backups.router, prefix="/api/v1")
app.include_router(questionnarium_router, prefix="/api/v1")
app.include_router(me.router, prefix="/api/v1")
