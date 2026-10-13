import uvicorn

from .app import build_app
from .config import Settings


def main() -> None:
    settings = Settings()
    uvicorn.run(build_app(settings), host="0.0.0.0", port=settings.port, proxy_headers=False)


if __name__ == "__main__":
    main()
