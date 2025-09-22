
# jinja_filters.py — Register extra Jinja filters for AWB Hub
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

def register_filters(templates, tz: str = "Europe/Bucharest"):
    """Attach commonly used filters to Jinja environment.
    Usage:
        from jinja_filters import register_filters
        templates = Jinja2Templates(directory="templates")
        register_filters(templates)
    """
    env = templates.env

    def localtime(value: Optional[datetime], tzname: str = tz):
        if not value:
            return value
        try:
            z = ZoneInfo(tzname)
        except Exception:
            z = ZoneInfo("UTC")
        if value.tzinfo is None:
            # assume UTC if naive
            value = value.replace(tzinfo=ZoneInfo("UTC"))
        return value.astimezone(z)

    def strftime(value: Optional[datetime], fmt: str = "%d-%m-%Y %H:%M"):
        try:
            return value.strftime(fmt) if value else ""
        except Exception:
            return ""

    env.filters.setdefault("localtime", localtime)
    env.filters["strftime"] = strftime
