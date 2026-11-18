"""`bin/hub-calib` 的入口（逻辑在 cli.main_calib）。"""
import sys

from .cli import main_calib

if __name__ == "__main__":
    sys.exit(main_calib())
