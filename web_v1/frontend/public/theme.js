// Sets the saved or system theme before the first paint, so no page flashes the wrong one.
(function () {
  var saved = null;
  try { saved = localStorage.getItem("craft-theme"); } catch (e) { /* private window */ }
  var light = saved ? saved === "light" : window.matchMedia("(prefers-color-scheme: light)").matches;
  document.documentElement.dataset.theme = light ? "light" : "dark";
  if (light) document.querySelector('meta[name="theme-color"]').setAttribute("content", "#f7f5f2");
})();
