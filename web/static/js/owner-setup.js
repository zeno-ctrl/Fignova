(function () {
  const f = document.getElementById("setup-form");
  f.addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = document.getElementById("setup-err");
    err.classList.add("hidden");
    try {
      const d = await AF.api("/api/owner/bootstrap", { method: "POST", json: { setup_key: document.getElementById("key").value } });
      location.href = d.enroll_url;
    } catch (ex) { err.textContent = ex.message; err.classList.remove("hidden"); }
  });
})();
