// Step the replicas number input in a node's scale form up/down by 1 (clamped at 0).
window.nodeStep = function (btn, delta) {
  var input = btn.closest('form').querySelector('input[name="replicas"]');
  if (!input) return;
  var next = (parseInt(input.value, 10) || 0) + delta;
  input.value = Math.max(0, next);
};
