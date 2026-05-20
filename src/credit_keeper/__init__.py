"""credit-keeper: config-driven header-mutating HTTP/HTTPS proxy."""

from .config import Config, ConfigError, HeaderOp, Rule, load_config
from .rules import CaseInsensitiveHeaders, apply_ops, rule_matches

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "Config",
    "ConfigError",
    "HeaderOp",
    "Rule",
    "load_config",
    "CaseInsensitiveHeaders",
    "apply_ops",
    "rule_matches",
]
