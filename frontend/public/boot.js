// UI-4: follow the OS preference unless the user picked a mode.
try {
  var stored = localStorage.getItem("rf.theme");
  var dark = stored ? stored === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
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
