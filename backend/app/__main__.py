"""``python -m app``: start the server and open the browser."""

import os
import socket
import threading
import webbrowser

import uvicorn


def _port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def _pick_port(host: str, preferred: int) -> int:
    """Use the preferred port, or the next free one if another app already uses it."""
    for port in range(preferred, preferred + 20):
        if _port_is_free(host, port):
            if port != preferred:
                print(f"Port {preferred} is busy, using {port} instead.")
            return port
    raise SystemExit(f"No free port found between {preferred} and {preferred + 19}.")


def main() -> None:
    host = os.environ.get("CLIPFORGE_HOST", "127.0.0.1")
    port = _pick_port(host, int(os.environ.get("CLIPFORGE_PORT", "8000")))
    url = f"http://localhost:{port}"
    print(f"\n  ClipForge is running at {url}\n  Press Ctrl+C to stop.\n")
    if os.environ.get("CLIPFORGE_OPEN_BROWSER", "1") == "1":
        threading.Timer(2.0, lambda: webbrowser.open(url)).start()
    uvicorn.run("app.main:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
