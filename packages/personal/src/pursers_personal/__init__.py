"""On Board Personal Preview public package."""

from importlib.metadata import PackageNotFoundError, version

PRODUCT_VERSION = "5.0.0a15"

try:
    __version__ = version("pursers-personal")
except PackageNotFoundError:
    __version__ = PRODUCT_VERSION


__all__ = ["PRODUCT_VERSION", "__version__"]
