// sorts.js (ES module)
// Usage:
// <script type="module">
//   import { initSort } from "/static/js/sorts.js";
//   initSort({ formSelector: "#filterForm", headerSelector: "[data-sort]" });
// </script>
//
// Expect headers like:
// <th data-sort="created_at">Data</th>
// Hidden inputs required (auto-created if missing): sort_by, sort_dir

function ensureHidden(form, name) {
  let el = form.querySelector(`input[name="${name}"]`);
  if (!el) {
    el = document.createElement("input");
    el.type = "hidden";
    el.name = name;
    form.appendChild(el);
  }
  return el;
}

export function initSort({ formSelector = "#filterForm", headerSelector = "[data-sort]" } = {}) {
  const form = document.querySelector(formSelector);
  if (!form) return;

  const sortBy = ensureHidden(form, "sort_by");
  const sortDir = ensureHidden(form, "sort_dir");

  document.querySelectorAll(headerSelector).forEach((th) => {
    th.style.cursor = "pointer";
    th.addEventListener("click", () => {
      const field = th.dataset.sort;
      if (!field) return;
      if (sortBy.value === field) {
        sortDir.value = sortDir.value === "asc" ? "desc" : "asc";
      } else {
        sortBy.value = field;
        sortDir.value = "asc";
      }
      form.submit();
    });
  });
}
