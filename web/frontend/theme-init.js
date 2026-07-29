(() => {
  try {
    const theme = localStorage.getItem("rdg-theme");
    if (theme) document.documentElement.setAttribute("data-theme", theme);
  } catch (_error) {
    // Storage may be disabled; the system theme remains a safe fallback.
  }
})();
