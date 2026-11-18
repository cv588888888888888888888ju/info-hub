"""`bin/hub-input` 的入口（逻辑在 cli.main_input）。"""
import sys

from .cli import main_input

if __name__ == "__main__":
    sys.exit(main_input())
