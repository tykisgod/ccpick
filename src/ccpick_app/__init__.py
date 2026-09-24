"""ccpick: Chrome profile selection and Claude account switching."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ccpick")
except PackageNotFoundError:
    __version__ = "0.1.0.dev0"
