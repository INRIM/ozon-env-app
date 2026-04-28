from uvicorn import run

from app.app_settings import get_env_settings

settings = get_env_settings()

if __name__ == "__main__":
    run("app.app:app", host=settings.asgi_host, port=settings.asgi_port)
