# dependencies.py — central place to expose the Jinja templates and helpers
from templating import get_templates  # reuse the single instance from templating.py

def get_pagination_numbers(current_page: int, total_pages: int, context_size: int = 2) -> list:
    """Generează o listă de numere de pagină pentru controalele de paginare.
    Exemplu: [1, '...', 4, 5, 6, '...', 12]
    """
    if total_pages <= 1:
        return []

    page_numbers = []
    start_page = max(1, current_page - context_size)
    end_page = min(total_pages, current_page + context_size)

    if start_page > 1:
        page_numbers.append(1)
        if start_page > 2:
            page_numbers.append('...')

    page_numbers.extend(range(start_page, end_page + 1))

    if end_page < total_pages:
        if end_page < total_pages - 1:
            page_numbers.append('...')
        page_numbers.append(total_pages)

    return page_numbers
