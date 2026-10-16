"""One bounded stdin/stdout exchange across an explicitly selected interpreter.

This is an OS-private local adapter, not a remotely authenticated network API.
Only a trusted host process with access to the authority database may invoke it.
"""

import argparse
import json
import sys

from .client import local_request
from .contracts import EventError, encode
from .operations import EventOperations


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    args = parser.parse_args()
    try:
        data = sys.stdin.buffer.read(4 * 1024 * 1024 + 1)
        if len(data) > 4 * 1024 * 1024:
            raise EventError("request_capacity")
        result = local_request(EventOperations(args.database), json.loads(data))
        print(encode({"ok": True, "result": result}))
    except (EventError, ValueError, KeyError, OSError) as exc:
        print(encode({"ok": False, "error": str(exc)}))
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
