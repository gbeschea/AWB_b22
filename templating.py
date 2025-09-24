from fastapi.templating import Jinja2Templates
from jinja_filters import register_filters

templates = Jinja2Templates(directory="templates")
register_filters(templates)  # atașează filtrele (localtime, strftime, slugify)


def qurl(request, name, **params):
    """
    Construiește un URL către ruta `name`, păstrând query-ul existent
    și suprascriind cheile cu valorile date în `params`.
    Elimină cheile când valoarea este None sau șir gol.
    """
    url = request.url_for(name)
    current = dict(request.query_params)  # query curent -> dict mutabil

    # update/ștergere chei
    for k, v in params.items():
        if v is None or v == "":
            current.pop(k, None)
        else:
            current[k] = v

    # include_query_params face encoding corect pentru toate valorile
    return str(url.include_query_params(**current))


# expune helper-ul în Jinja
templates.env.globals["qurl"] = qurl


def get_templates():
    return templates
