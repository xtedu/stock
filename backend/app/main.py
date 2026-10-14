import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import scheduler
from .api import alerts, insights, jobs, pool, stocks, zones
from .config import get_settings

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = FastAPI(title="AI Stock Picker", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(pool.router)
app.include_router(stocks.router)
app.include_router(zones.router)
app.include_router(alerts.router)
app.include_router(insights.router)
app.include_router(jobs.router)


@app.on_event("startup")
def on_startup():
    scheduler.start()


@app.on_event("shutdown")
def on_shutdown():
    scheduler.shutdown()


@app.get("/api/health")
def health():
    return {"status": "ok"}
