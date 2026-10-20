/* TV Home lite: feature checks for older TVs, run before the page builds.
   Flexbox gap (Chromium 84+) cannot be tested with @supports: measure it and
   set html.no-flexgap for lite/css/compat.css. ES5. */
(function () {
  "use strict";
  var d = document.createElement("div");
  d.style.cssText = "display:flex;flex-direction:column;gap:1px;position:absolute;visibility:hidden";
  d.appendChild(document.createElement("div"));
  d.appendChild(document.createElement("div"));
  document.documentElement.appendChild(d);
  var ok = d.scrollHeight === 1;
  document.documentElement.removeChild(d);
  if (!ok) document.documentElement.className += " no-flexgap";
})();
