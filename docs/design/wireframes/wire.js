// SPDX-License-Identifier: AGPL-3.0-or-later
// Wireframe page helper: light/dark toggle. Not @jesinia/design source.
(function () {
  var root = document.documentElement;
  var btn = document.querySelector(".theme-toggle");
  if (!btn) return;
  function label() {
    var t = root.getAttribute("data-theme");
    btn.textContent = t === "dark" ? "Theme: dark" : t === "light" ? "Theme: light" : "Theme: system";
  }
  btn.addEventListener("click", function () {
    var t = root.getAttribute("data-theme");
    var next = t === null ? "light" : t === "light" ? "dark" : null;
    if (next) root.setAttribute("data-theme", next); else root.removeAttribute("data-theme");
    label();
  });
  label();
})();
