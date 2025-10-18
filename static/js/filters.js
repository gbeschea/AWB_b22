// filters.js  (ES module)
// Usage:
// <script type="module">
//   import { initFilters } from "/static/js/filters.js";
//   initFilters("#filterForm");
// </script>

function debounce(fn, wait = 400) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn.apply(null, args), wait);
  };
}

function submitForm(form) {
  // if there is a hidden "select_all_filtered_confirm", reset it on any manual change
  const confirm = form.querySelector('#select_all_filtered_confirm');
  if (confirm) confirm.value = 'false';
  form.submit();
}

export function initFilters(formSelector = "#filterForm") {
  const form = document.querySelector(formSelector);
  if (!form) return;

  const debouncedSubmit = debounce(() => submitForm(form), 500);

  // Change on selects and dates -> submit
  form.querySelectorAll('select, input[type="date"]').forEach(el => {
    el.addEventListener('change', debouncedSubmit);
  });

  // Typing in text inputs -> debounced submit
  form.querySelectorAll('input[type="text"], input[type="search"]').forEach(el => {
    el.addEventListener('input', debouncedSubmit);
    el.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        submitForm(form);
      }
    });
  });

  // Reset button -> clear and submit
  const resetBtn = form.querySelector('button[type="reset"], [data-reset-filters]');
  if (resetBtn) {
    resetBtn.addEventListener('click', () => {
      // let the form reset first
      setTimeout(() => submitForm(form), 0);
    });
  }
}
