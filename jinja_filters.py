# jinja_filters.py — Register extra Jinja filters for AWB Hub
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo
import re
import unicodedata

def _strip_accents(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    return "".join(ch for ch in s if unicodedata.category(ch) != "Mn")

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

    def slugify(value):
        if not value: return ""
        v = _strip_accents(str(value)).lower()
        v = re.sub(r"[^a-z0-9\\-\\s_]+", "", v)
        v = re.sub(r"[\\s_]+", "-", v).strip("-")
        return re.sub(r"-{2,}", "-", v)
    env.filters["slugify"] = slugify


    env.filters.setdefault("localtime", localtime)
    env.filters["strftime"] = strftime
    env.filters["slugify"] = slugify


