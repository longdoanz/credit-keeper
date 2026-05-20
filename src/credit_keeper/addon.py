"""mitmproxy addon that mutates headers per a credit-keeper YAML config."""

from __future__ import annotations

import logging
from pathlib import Path

from mitmproxy import ctx

from .config import Config, load_config
from .rules import apply_ops, rule_matches

logger = logging.getLogger(__name__)


class HeaderCustomizer:
    """mitmproxy addon: applies header rules from a YAML config to flows.

    The addon is normally constructed in-process by the credit-keeper CLI with
    an explicit config path. It also supports being loaded as a script addon
    (``mitmdump -s addon.py --set config=path/to/config.yaml``) via the
    ``load`` and ``configure`` hooks.
    """

    def __init__(self, config_path: str | Path | None = None) -> None:
        self.config_path: Path | None = Path(config_path) if config_path else None
        self.config: Config = Config(rules=[])
        if self.config_path is not None:
            self._reload()

    # ------------------------------------------------------------------
    # Config loading
    # ------------------------------------------------------------------

    def _reload(self) -> None:
        assert self.config_path is not None
        self.config = load_config(self.config_path)
        logger.info(
            "credit-keeper: loaded %d rule(s) from %s",
            len(self.config.rules),
            self.config_path,
        )

    # ------------------------------------------------------------------
    # mitmproxy lifecycle hooks
    # ------------------------------------------------------------------

    def load(self, loader) -> None:  # type: ignore[no-untyped-def]
        loader.add_option(
            name="config",
            typespec=str,
            default="",
            help="Path to the credit-keeper YAML config",
        )

    def configure(self, updates) -> None:  # type: ignore[no-untyped-def]
        if "config" not in updates:
            return
        path = getattr(ctx.options, "config", "") or ""
        if not path:
            return
        new_path = Path(path)
        if self.config_path == new_path and self.config.rules:
            return
        self.config_path = new_path
        try:
            self._reload()
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("credit-keeper: failed to load config %s: %s", new_path, exc)

    # ------------------------------------------------------------------
    # Flow hooks
    # ------------------------------------------------------------------

    def request(self, flow) -> None:  # type: ignore[no-untyped-def]
        self._apply(flow, direction="request")

    def response(self, flow) -> None:  # type: ignore[no-untyped-def]
        self._apply(flow, direction="response")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _apply(self, flow, *, direction: str) -> None:  # type: ignore[no-untyped-def]
        if not self.config.rules:
            return

        req = flow.request
        host = req.pretty_host
        url = req.pretty_url
        method = req.method

        if direction == "request":
            headers = req.headers
        else:
            if flow.response is None:
                return
            headers = flow.response.headers

        for rule in self.config.rules:
            try:
                if not rule_matches(
                    rule,
                    host=host,
                    url=url,
                    method=method,
                    direction=direction,  # type: ignore[arg-type]
                ):
                    continue
                ops = rule.request if direction == "request" else rule.response
                apply_ops(ops, headers)
            except Exception as exc:
                logger.warning(
                    "credit-keeper: rule %r failed on %s %s: %s",
                    rule.name,
                    direction,
                    url,
                    exc,
                )
