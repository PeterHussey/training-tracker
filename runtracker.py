"""Launch the training-tracker dashboard.

    uv run python runtracker.py [streamlit args...]

Extra arguments are forwarded to ``streamlit run`` (e.g. ``--server.port 8600``).
The app itself is ``dashboard.py``; this file only starts a server for it.
"""

import subprocess
import sys
from pathlib import Path

APP = Path(__file__).resolve().with_name("dashboard.py")


def main() -> int:
    """Run ``streamlit run dashboard.py`` in this interpreter, args passed through."""
    return subprocess.call([sys.executable, "-m", "streamlit", "run", str(APP), *sys.argv[1:]])


if __name__ == "__main__":
    sys.exit(main())
