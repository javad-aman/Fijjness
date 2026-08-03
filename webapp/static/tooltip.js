// Single delegated hover listener for every server-rendered SVG chart on the
// page - each bar/point carries a data-tip attribute (see webapp/charts.py's
// _tip helper); this just positions the shared #tip element next to the
// cursor. No per-chart JS, no charting library.
(() => {
  const tip = document.getElementById("tip");
  if (!tip) return;

  document.addEventListener("mousemove", (e) => {
    const target = e.target.closest("[data-tip]");
    if (!target) {
      tip.style.opacity = 0;
      return;
    }
    tip.textContent = target.getAttribute("data-tip");
    tip.style.opacity = 1;
    tip.style.left = e.clientX + 14 + "px";
    tip.style.top = e.clientY - 8 + "px";
  });

  document.addEventListener("mouseleave", () => {
    tip.style.opacity = 0;
  });
})();
