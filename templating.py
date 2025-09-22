from fastapi.templating import Jinja2Templates
from jinja_filters import register_filters

templates = Jinja2Templates(directory="templates")
register_filters(templates)  # atașează filtrele (localtime, strftime, slugify)

def get_templates():
    return templates
