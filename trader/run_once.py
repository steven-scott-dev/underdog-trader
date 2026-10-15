import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trader.core import run_pass


def main():
    res = run_pass()
    summary = {k: v for k, v in res.items() if k != "log"}
    print(json.dumps(summary, indent=2, default=str))
    print("--- log ---")
    print("\n".join(res.get("log", [])))


if __name__ == "__main__":
    main()
