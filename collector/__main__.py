from collector.app import app
import os
import uvicorn


def main():
    host = os.environ.get("COLLECTOR_HOST", "0.0.0.0")
    port = int(os.environ.get("COLLECTOR_PORT", "8080"))
    uvicorn.run(app, host=host, port=port, proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
