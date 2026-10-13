import uvicorn

from .main import create_app


def main() -> None:
    uvicorn.run(create_app(), host="0.0.0.0", port=8000, proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
