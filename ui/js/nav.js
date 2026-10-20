/* Spatial focus for the remote. Sections ([data-section]) stack vertically in
   DOM order; items ([data-item]) inside a section run horizontally, or wrap
   into a grid when the section carries data-cols. Up/Down move between grid
   rows and then between sections, keeping the column; Left/Right change the
   item within the visual row. Entering a section picks the item nearest on
   the x axis (first or last row of a grid); the doors always land on the
   selected tab.

   Only the open room's stage takes part (setScope); the other rooms' stages
   stay built but out of reach. Item positions are measured in rebuild(), from
   layout offsets (transforms ignored), so a key press never reads layout. */
window.TVH = window.TVH || {};
TVH.nav = (function () {
  "use strict";
  var all = [];        // every section on the page
  var sections = [];   // the ones in reach: outside any stage, or in the scope stage
  var scope = null;
  var cur = null;
  var onFocus = [];

  /* Page position of an element's box from offsetLeft/offsetTop, which ignore
     transforms: stable while things slide or scale. */
  function pos(el) {
    var x = 0, y = 0;
    for (var e = el; e; e = e.offsetParent) { x += e.offsetLeft; y += e.offsetTop; }
    return { x: x, y: y };
  }

  /* Rescan the page (after a room was built, or fonts changed widths). Keeps
     the current item when it is still there. Reads layout: never call it
     from a key handler or during an animation. */
  function rebuild() {
    var curEl = current();
    all = [];
    var els = document.querySelectorAll("[data-section]");
    for (var i = 0; i < els.length; i++) {
      var items = Array.prototype.slice.call(els[i].querySelectorAll("[data-item]"));
      if (!items.length) continue;
      var cols = +(els[i].dataset.cols || 0) || items.length;
      var stage = els[i].closest(".stage");
      var sec = { el: els[i], kind: els[i].dataset.section, items: items, cols: cols, stage: stage, index: 0,
                  order: (+(els[i].dataset.order || 1)) * 1000 + i, x: [], y: [] };
      items.forEach(function (it) {
        var p = pos(it);
        sec.x.push(p.x + it.offsetWidth / 2);
        sec.y.push(p.y);
      });
      if (curEl && items.indexOf(curEl) >= 0) sec.index = items.indexOf(curEl);
      all.push(sec);
    }
    all.sort(function (a, b) { return a.order - b.order; });
    cur = null;
    for (var k = 0; k < all.length; k++) if (curEl && all[k].items[all[k].index] === curEl) cur = all[k];
    filter();
    if (!cur && curEl) home();
  }

  function filter() {
    sections = all.filter(function (s) { return !s.stage || s.stage === scope; });
  }

  /* Which stage is in reach. Cheap: no DOM reads. */
  function setScope(stage) {
    scope = stage;
    filter();
  }

  function current() { return cur ? cur.items[cur.index] : null; }

  function setFocus(sec, i) {
    var prev = current();
    if (prev && prev !== sec.items[i]) prev.classList.remove("focus");
    if (cur && cur !== sec) cur.el.classList.remove("focus");
    cur = sec;
    sec.index = i;
    var el = sec.items[i];
    el.classList.add("focus");
    sec.el.classList.add("focus");
    for (var k = 0; k < onFocus.length; k++) onFocus[k](sec, el, i);
  }

  /* Index of the item in `to` nearest on the x axis to the current item,
     searched between lo and hi (exclusive). */
  function nearestX(to, lo, hi) {
    if (!cur) return lo;
    var cx = cur.x[cur.index];
    var best = lo, bd = Infinity;
    for (var i = lo; i < hi; i++) {
      var d = Math.abs(to.x[i] - cx);
      if (d < bd) { bd = d; best = i; }
    }
    return best;
  }

  function rowOf(sec, i) { return Math.floor(i / sec.cols); }
  function rowCount(sec) { return Math.ceil(sec.items.length / sec.cols); }

  function move(dir) {
    if (!sections.length) return false;
    if (!cur) { home(); return true; }
    var s = sections.indexOf(cur), n = cur.items.length;
    if (dir === "left" || dir === "right") {
      var ni = cur.index + (dir === "left" ? -1 : 1);
      if (ni < 0 || ni >= n || rowOf(cur, ni) !== rowOf(cur, cur.index)) return false;
      setFocus(cur, ni);
      return true;
    }
    // Inside a grid, first try the row above or below in the same column.
    var row = rowOf(cur, cur.index), nrow = row + (dir === "up" ? -1 : 1);
    if (nrow >= 0 && nrow < rowCount(cur)) {
      setFocus(cur, Math.min(cur.index + (dir === "up" ? -cur.cols : cur.cols), n - 1));
      return true;
    }
    var ns = s + (dir === "up" ? -1 : 1);
    if (ns < 0 || ns >= sections.length) return false;
    var target = sections[ns], m = target.items.length, i;
    if (target.kind === "doors") {
      // Back up into the doors always lands on the selected tab, never the nearest door.
      i = Math.min(homeIdx, m - 1);
    } else if (dir === "down") {
      // The hero is a wide left-aligned block: down from it starts the grid at its first column.
      i = cur.kind === "hero" ? 0 : nearestX(target, 0, Math.min(target.cols, m));
    } else {
      i = nearestX(target, rowOf(target, m - 1) * target.cols, m);
    }
    setFocus(target, i);
    return true;
  }

  function focusEl(el) {
    for (var s = 0; s < sections.length; s++) {
      var i = sections[s].items.indexOf(el);
      if (i >= 0) { setFocus(sections[s], i); return true; }
    }
    return false;
  }

  /* Home is the doors section; which door is remembered (the selected tab). */
  var homeIdx = 0;
  function home() {
    if (!sections.length) return;
    var s = 0;
    for (var i = 0; i < sections.length; i++) if (sections[i].kind === "doors") { s = i; break; }
    setFocus(sections[s], Math.min(homeIdx, sections[s].items.length - 1));
  }
  function setHome(i) { homeIdx = i; }

  function focusSection(kind, index) {
    for (var i = 0; i < sections.length; i++) {
      if (sections[i].kind === kind) { setFocus(sections[i], Math.min(index || 0, sections[i].items.length - 1)); return true; }
    }
    return false;
  }

  return {
    rebuild: rebuild, setScope: setScope, move: move, focusEl: focusEl, current: current, home: home,
    setHome: setHome, focusSection: focusSection, rowOf: rowOf,
    section: function () { return cur; },
    sections: function () { return sections; },
    onFocus: function (fn) { onFocus.push(fn); }
  };
})();
