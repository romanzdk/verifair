(() => {
  const nav = document.querySelector("[data-status-nav]");
  const active = nav?.querySelector('[aria-current="page"]');
  if (active && nav.scrollWidth > nav.clientWidth) {
    nav.scrollLeft = Math.max(0, active.offsetLeft - 16);
  }
  document.querySelectorAll("[data-copy-identity]").forEach(button => {
    button.addEventListener("click", async () => {
      const result = button.parentElement.querySelector("[data-copy-result]");
      try {
        await navigator.clipboard.writeText(button.dataset.copyIdentity);
        result.textContent = "Identity copied.";
      } catch {
        result.textContent = "Select the ID above to copy it.";
      }
    });
  });
})();
