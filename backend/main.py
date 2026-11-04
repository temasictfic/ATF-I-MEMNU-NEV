import argparse
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from config import settings
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    print(f"Atf-ı Memnu backend starting on port {settings.port}")
    Path(settings.output_dir).mkdir(parents=True, exist_ok=True)

    # Preload NER model in the background so the first extraction is instant
    import asyncio

    from services.ner_model_manager import preload_pipeline
    asyncio.create_task(preload_pipeline())

    yield
    # Shutdown
    print("Atf-ı Memnu backend shutting down")
    from services.ner_model_manager import shutdown_inference_executor
    from verifiers._http import close_session
    await close_session()
    shutdown_inference_executor()


app = FastAPI(title="Atf-ı Memnu API", version="1.0.0", lifespan=lifespan)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Import and include routers
from api.router import api_router

app.include_router(api_router, prefix="/api")


if __name__ == "__main__":
    import server_state

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument("--host", type=str, default=settings.host)
    args = parser.parse_args()

    settings.port = args.port

    config = uvicorn.Config(
        app,
        host=args.host,
        port=args.port,
        reload=False,
        log_level="info",
        ws="websockets-sansio",
    )
    server_state.uvicorn_server = uvicorn.Server(config)
    server_state.uvicorn_server.run()
