"""Command-line entry point for credit-keeper."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from . import __version__
from .addon import HeaderCustomizer


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="credit-keeper",
        description=(
            "Run a header-mutating HTTP/HTTPS proxy driven by a YAML config. "
            "Built on top of mitmproxy."
        ),
    )
    parser.add_argument(
        "-c",
        "--config",
        required=True,
        type=Path,
        help="Path to the YAML config file describing header rules",
    )
    parser.add_argument(
        "--listen-host",
        default="127.0.0.1",
        help="Address to bind the proxy on (default: 127.0.0.1)",
    )
    parser.add_argument(
        "-p",
        "--listen-port",
        type=int,
        default=8080,
        help="Port to listen on (default: 8080)",
    )
    parser.add_argument(
        "--mode",
        default="regular",
        help=(
            "mitmproxy proxy mode (default: regular). Examples: regular, "
            "transparent, socks5, reverse:https://example.com, upstream:..."
        ),
    )
    parser.add_argument(
        "--ssl-insecure",
        action="store_true",
        help="Do not verify upstream TLS certificates",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"credit-keeper {__version__}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Imported lazily so '--help' and '--version' do not pay the startup cost.
    from mitmproxy.options import Options
    from mitmproxy.tools.dump import DumpMaster

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    addon = HeaderCustomizer(config_path=args.config)

    print(
        f"credit-keeper listening on {args.listen_host}:{args.listen_port}, "
        f"loaded {len(addon.config.rules)} rules from {args.config}",
        flush=True,
    )

    async def _run() -> None:
        opts = Options(
            listen_host=args.listen_host,
            listen_port=args.listen_port,
            mode=[args.mode],
            ssl_insecure=args.ssl_insecure,
        )
        master = DumpMaster(opts)
        master.addons.add(addon)
        try:
            await master.run()
        except KeyboardInterrupt:
            master.shutdown()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
