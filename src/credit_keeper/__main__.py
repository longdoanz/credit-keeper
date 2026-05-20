"""Allow ``python -m credit_keeper`` as an alias for the ``credit-keeper`` CLI."""

from .cli import main

raise SystemExit(main())
