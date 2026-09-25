"""Start the web UI: ``python -m mapillary_downloader``."""

import argparse
import webbrowser

from .server import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Mapillary area downloader (web UI)")
    parser.add_argument("--host", default="127.0.0.1", help="interface to listen on (default: %(default)s)")
    parser.add_argument("--port", type=int, default=8000, help="port (default: %(default)s)")
    parser.add_argument("--output", default="downloads", help="folder for downloaded areas (default: %(default)s)")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    args = parser.parse_args()

    app = create_app(output_root=args.output)
    url = f"http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{args.port}/"
    print(f"Mapillary downloader running at {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        webbrowser.open(url)
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
