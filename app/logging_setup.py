import json
import logging
import sys
import time


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "ts": round(time.time(), 3),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = value
        return json.dumps(payload)


def configure_logging(level="INFO"):
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # Quiet gunicorn/werkzeug access-log noise at DEBUG; keep errors visible.
    logging.getLogger("werkzeug").setLevel(max(logging.WARNING, root.level))
