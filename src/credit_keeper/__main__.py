"""Allow ``python -m credit_keeper`` as an alias for the ``credit-keeper`` CLI."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
