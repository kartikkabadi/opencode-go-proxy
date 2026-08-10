import os
import sys

from . import catalog


def main() -> None:
    if "--refresh-catalog" in sys.argv or os.environ.get("OPENCODE_GO_PROXY_REFRESH_CATALOG") == "1":
        argv = [a for a in sys.argv[1:] if a != "--refresh-catalog"]
        sys.exit(catalog.main_refresh(argv))
    from .app import main as app_main

    app_main()


if __name__ == "__main__":
    main()
