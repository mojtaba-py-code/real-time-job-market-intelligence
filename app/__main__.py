"""Allow ``python -m app`` to run the CLI."""

from __future__ import annotations

from app.cli.main import main

if __name__ == "__main__":
    main()
