"""awewarm — keep AI coding-plan subscription windows warm."""
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("awewarm")
except PackageNotFoundError:
    __version__ = "0.0.0"
