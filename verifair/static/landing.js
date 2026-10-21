(() => {
  const nav = document.querySelector("[data-status-nav]");
  const active = nav?.querySelector('[aria-current="page"]');
  if (active && nav.scrollWidth > nav.clientWidth) {
    nav.scrollLeft = Math.max(0, active.offsetLeft - 16);
  }
})();
