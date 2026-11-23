/* Landing page: gentle reveal on scroll + before/after comparison slider. */
(function () {
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const items = document.querySelectorAll(".reveal");
  if (reduce || !("IntersectionObserver" in window)) {
    items.forEach((el) => el.classList.add("in"));
  } else {
    const io = new IntersectionObserver((entries) => {
      entries.forEach((en) => { if (en.isIntersecting) { en.target.classList.add("in"); io.unobserve(en.target); } });
    }, { rootMargin: "0px 0px -8% 0px", threshold: 0.08 });
    items.forEach((el, i) => { el.style.transitionDelay = (Math.min(i % 6, 5) * 60) + "ms"; io.observe(el); });
  }
  const box = document.getElementById("compare2");
  const range = document.getElementById("compare-range");
  if (box && range) {
    const set = () => box.style.setProperty("--pos", range.value + "%");
    range.addEventListener("input", set);
    set();
    if (!reduce) {  // a short hint animation the first time the slider is seen
      const io2 = new IntersectionObserver((en) => {
        if (!en[0].isIntersecting) return;
        io2.disconnect();
        let t = 0;
        const tick = () => { t += 1; range.value = String(34 + Math.sin(t / 14) * 20 * Math.max(0, 1 - t / 90)); set(); if (t < 90) requestAnimationFrame(tick); else { range.value = "34"; set(); } };
        requestAnimationFrame(tick);
      }, { threshold: 0.5 });
      io2.observe(box);
    }
  }
})();
