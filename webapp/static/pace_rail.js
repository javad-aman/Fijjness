// Animate each pace rail's fill from 0 to its actual width on load.
// Respects prefers-reduced-motion by skipping the animation class entirely
// (the CSS fallback also guards this, but skipping avoids any layout thrash).
document.addEventListener("DOMContentLoaded", () => {
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const fills = document.querySelectorAll(".pace-rail-fill");

  fills.forEach((fill) => {
    if (reduceMotion) {
      fill.style.width = "var(--fill-pct)";
      return;
    }
    // Force a reflow before adding the class so the transition actually runs
    // from 0 instead of jumping straight to the final width.
    void fill.offsetWidth;
    requestAnimationFrame(() => fill.classList.add("is-animated"));
  });
});
