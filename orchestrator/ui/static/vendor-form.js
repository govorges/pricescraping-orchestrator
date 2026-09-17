// Selector-builder helpers for the vendor add/edit forms.
// The Required / Ignore-filter checkboxes carry the row's field NAME as their
// value so the server can pair them up regardless of row order; keep that value
// in sync with the name input, including at submit time.
(function () {
  function syncRow(row) {
    var name = row.querySelector('input[name="sel_name"]');
    if (!name) return;
    row.querySelectorAll('input[name="sel_required"], input[name="sel_ignore"]').forEach(function (cb) {
      cb.value = name.value.trim();
    });
  }

  window.vendorFormAddRow = function (btn) {
    var form = btn.closest('form');
    var tpl = form.querySelector('template.selrow-tpl');
    var body = form.querySelector('tbody.selbody');
    if (!tpl || !body) return;
    body.appendChild(tpl.content.cloneNode(true));
  };

  document.addEventListener('input', function (e) {
    if (e.target && e.target.name === 'sel_name') syncRow(e.target.closest('tr'));
  });

  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form.querySelector || !form.querySelector('tbody.selbody')) return;
    form.querySelectorAll('tr.selrow').forEach(syncRow);
  }, true);
})();
