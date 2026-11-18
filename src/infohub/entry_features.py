"""`bin/hub-features` 的入口（逻辑在 cli.main_features）。"""
import sys

from .cli import main_features

if __name__ == "__main__":
    sys.exit(main_features())
