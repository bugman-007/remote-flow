// UI-4: dark by default; "system" follows the OS, and a picked mode sticks.
try {
  var stored = localStorage.getItem("rf.theme");
  var dark = stored === "light" ? false : stored === "system" ? window.matchMedia("(prefers-color-scheme: dark)").matches : true;
  document.documentElement.classList.toggle("dark", dark);
} catch (_) {}

// UI-7: a stale bundle (deploy mid-session) reloads once on a chunk-load error.
window.addEventListener("error", function (event) {
  if (event && event.message && /Importing a module script failed|Loading chunk/.test(event.message)) {
    if (!sessionStorage.getItem("rf.reloaded")) {
      sessionStorage.setItem("rf.reloaded", "1");
      location.reload();
    }
  }
});
window.addEventListener("load", function () {
  sessionStorage.removeItem("rf.reloaded");
});
