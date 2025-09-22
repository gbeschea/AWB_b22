from fastapi.templating import Jinja2Templates
from babel.dates import format_datetime
import pytz
from slugify import slugify

def to_localtime(utc_dt, tz='Europe/Bucharest'):
    """Convertește un datetime din UTC în fusul orar local."""
    if utc_dt is None:
        return None
    local_tz = pytz.timezone(tz)
    # Dacă e naiv, îl considerăm UTC (nu suprascrie tz dacă e deja aware)
    if getattr(utc_dt, "tzinfo", None) is None:
        utc_dt = utc_dt.replace(tzinfo=pytz.utc)
    return utc_dt.astimezone(local_tz)

def format_datetime_local(utc_dt, format='medium', tz='Europe/Bucharest'):
    """Formatează direct un datetime UTC în string local (Babel)."""
    local_dt = to_localtime(utc_dt, tz)
    if local_dt is None:
        return ""
    return format_datetime(local_dt, format=format, locale='ro_RO')

def strftime_safe(dt, fmt='%d-%m-%Y %H:%M', tz='Europe/Bucharest'):
    """Filtru Jinja: {{ x | localtime | strftime('%d-%m-%Y %H:%M') }}."""
    if dt is None:
        return ""
    if getattr(dt, "tzinfo", None) is None:
        dt = dt.replace(tzinfo=pytz.utc)
    try:
        dt = dt.astimezone(pytz.timezone(tz))
    except Exception:
        pass
    try:
        return dt.strftime(fmt)
    except Exception:
        return ""

templates = Jinja2Templates(directory="templates")

# Filtre disponibile în tot proiectul
env = templates.env
env.filters['datetime_local'] = format_datetime_local
env.filters['localtime'] = to_localtime
env.filters['slugify'] = slugify
env.filters['strftime'] = strftime_safe
