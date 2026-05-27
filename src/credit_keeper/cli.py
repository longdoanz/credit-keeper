"""Command-line entry point for credit-keeper."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import logging
import socket
import sys
from pathlib import Path

import yaml

from . import __version__
from .addon import HeaderCustomizer
from .config import ConfigError

logger = logging.getLogger(__name__)


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
        "--db",
        type=Path,
        default=Path("./credit-keeper.db"),
        help="Path to the SQLite database for credential pool (default: ./credit-keeper.db)",
    )
    parser.add_argument(
        "--webui-port",
        type=int,
        default=None,
        help="Port to run the web UI dashboard on (disabled if not set)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"credit-keeper {__version__}",
    )
    return parser


def _is_loopback_host(host: str) -> bool:
    """Return True if ``host`` resolves only to loopback addresses."""
    candidates: list[str] = []
    try:
        candidates.append(str(ipaddress.ip_address(host)))
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror:
            # Unresolvable: be conservative and treat as non-loopback so we
            # warn the user something is off.
            return False
        candidates.extend(info[4][0] for info in infos)
    if not candidates:
        return False
    for addr in candidates:
        try:
            if not ipaddress.ip_address(addr).is_loopback:
                return False
        except ValueError:
            return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # Construct the addon eagerly so config errors surface as a clean CLI
    # error message instead of a Python traceback.
    try:
        addon = HeaderCustomizer(
            config_path=args.config,
            listen_host=args.listen_host,
            listen_port=args.listen_port,
        )
    except FileNotFoundError as exc:
        print(f"credit-keeper: error: config file not found: {exc.filename or args.config}", file=sys.stderr)
        return 2
    except yaml.YAMLError as exc:
        print(f"credit-keeper: error: invalid YAML in {args.config}: {exc}", file=sys.stderr)
        return 2
    except ConfigError as exc:
        print(f"credit-keeper: error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        # Permission denied, is-a-directory, etc.
        print(f"credit-keeper: error: cannot read {args.config}: {exc}", file=sys.stderr)
        return 2

    if not _is_loopback_host(args.listen_host):
        logger.warning(
            "credit-keeper: --listen-host=%s binds outside the loopback range; "
            "this is an authless intercepting proxy. Ensure access is restricted "
            "and remove the mitmproxy CA cert when finished.",
            args.listen_host,
        )

    # Imported lazily so '--help' and '--version' do not pay the startup cost.
    from mitmproxy.options import Options
    from mitmproxy.tools.dump import DumpMaster

    from .db import CredentialDB
    from .pool_addon import CredentialPoolAddon

    async def _run() -> None:
        opts = Options(
            listen_host=args.listen_host,
            listen_port=args.listen_port,
            mode=[args.mode],
            ssl_insecure=args.ssl_insecure,
        )
        master = DumpMaster(opts)
        master.addons.add(addon)

        # Wire up credential pool addon if enabled
        if (
            addon.config.credential_pool is not None
            and addon.config.credential_pool.enabled
        ):
            db = CredentialDB(args.db)
            pool_addon = CredentialPoolAddon(addon.config.credential_pool, db)
            master.addons.add(pool_addon)
            logger.info(
                "credit-keeper: credential pool enabled, intercepting %s",
                addon.config.credential_pool.intercept_hosts,
            )

        # Start web UI if requested
        if args.webui_port is not None:
            import threading

            import uvicorn

            from .webui.app import create_app

            webui_threshold = 0.0
            if addon.config.credential_pool is not None:
                webui_threshold = addon.config.credential_pool.warning_threshold_pct
            webui_app = create_app(str(args.db), warning_threshold_pct=webui_threshold)
            uvicorn_config = uvicorn.Config(
                webui_app,
                host=args.listen_host,
                port=args.webui_port,
                log_level="info",
            )
            server = uvicorn.Server(uvicorn_config)
            webui_thread = threading.Thread(target=server.run, daemon=True)
            webui_thread.start()
            logger.info(
                "credit-keeper: web UI started on http://%s:%d",
                args.listen_host,
                args.webui_port,
            )

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
