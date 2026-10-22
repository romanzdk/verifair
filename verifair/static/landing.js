(() => {
  const nav = document.querySelector("[data-status-nav]");
  const active = nav?.querySelector('[aria-current="page"]');
  if (active && nav.scrollWidth > nav.clientWidth) {
    nav.scrollLeft = Math.max(0, active.offsetLeft - 16);
  }
  document.querySelectorAll("[data-copy-identity], [data-copy-target]").forEach(button => {
    const field = button.dataset.copyTarget && document.getElementById(button.dataset.copyTarget);
    button.addEventListener("click", async () => {
      const result = button.parentElement.querySelector("[data-copy-result]");
      try {
        await navigator.clipboard.writeText(field ? field.value : button.dataset.copyIdentity);
        result.textContent = field ? "Token copied." : "Identity copied.";
      } catch {
        field?.select();
        result.textContent = field ? "Select the token above to copy it." : "Select the ID above to copy it.";
      }
    });
  });
  // Browsers skip autofocus when the URL has a fragment, and scrolling to the fragment moves
  // focus, so focus the field once that is done
  addEventListener("load", () => document.querySelector("[autofocus]")?.focus());
  document.addEventListener("submit", event => {
    const message = event.submitter?.dataset.confirm;
    if (message && !confirm(message)) event.preventDefault();
  });
})();
