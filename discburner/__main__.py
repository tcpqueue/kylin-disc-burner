import sys

if "--selftest" in sys.argv:
    from .selftest import run
    i = sys.argv.index("--selftest")
    sys.exit(run(sys.argv[i + 1] if len(sys.argv) > i + 1 else None))

from .gui import main

sys.exit(main())
