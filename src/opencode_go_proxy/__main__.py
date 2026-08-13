import os
import sys

from . import catalog, ops


def main() -> None:
    argv = sys.argv[1:]
    if "--refresh-catalog" in argv or os.environ.get("OPENCODE_GO_PROXY_REFRESH_CATALOG") == "1":
        argv = [a for a in argv if a != "--refresh-catalog"]
        sys.exit(catalog.main_refresh(argv))
    if argv and argv[0] == "doctor":
        sys.exit(ops.doctor(argv[1:]))
    if argv and argv[0] == "smoke-test":
        sys.exit(ops.smoke_test())
    if argv and argv[0] == "support-bundle":
        sys.exit(ops.support_bundle(argv[1:]))
    from .app import main as app_main

    app_main()


if __name__ == "__main__":
    main()
