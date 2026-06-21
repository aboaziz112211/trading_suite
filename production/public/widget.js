/* A2A embeddable chat widget — one-line install on any site.
 *
 *   <script src="https://YOUR-ASSISTANT.onrender.com/widget.js"
 *           data-accent="#16c784" data-position="right" defer></script>
 *
 * Injects a floating bubble + an iframe that loads the chat from the assistant
 * origin (so /api/chat stays same-origin → no CORS, no style clashes).
 */
(function () {
  var self =
    document.currentScript ||
    (function () { var s = document.getElementsByTagName("script"); return s[s.length - 1]; })();
  var base = new URL(self.src).origin;
  var accent = self.getAttribute("data-accent") || "#16c784";
  var side = self.getAttribute("data-position") === "left" ? "left" : "right";
  var z = 2147483000;

  var btn = document.createElement("button");
  btn.setAttribute("aria-label", "AI Assistant");
  btn.style.cssText =
    "position:fixed;bottom:20px;" + side + ":20px;width:60px;height:60px;border-radius:50%;" +
    "border:none;cursor:pointer;background:" + accent + ";color:#fff;font-size:26px;" +
    "box-shadow:0 8px 24px rgba(0,0,0,.28);z-index:" + z + ";display:flex;align-items:center;" +
    "justify-content:center;transition:transform .15s ease";
  btn.innerHTML = "💬";
  btn.onmouseenter = function () { btn.style.transform = "scale(1.06)"; };
  btn.onmouseleave = function () { btn.style.transform = "scale(1)"; };

  var frame = document.createElement("iframe");
  frame.src = base + "/embed.html";
  frame.title = "AI Assistant";
  frame.style.cssText =
    "position:fixed;bottom:92px;" + side + ":20px;width:420px;height:640px;" +
    "max-width:calc(100vw - 32px);max-height:calc(100vh - 120px);border:none;border-radius:18px;" +
    "box-shadow:0 24px 60px rgba(0,0,0,.32);z-index:" + z + ";display:none;background:transparent";

  var open = false;
  function toggle() {
    open = !open;
    frame.style.display = open ? "block" : "none";
    btn.innerHTML = open ? "✕" : "💬";
  }
  btn.addEventListener("click", toggle);

  function mount() {
    document.body.appendChild(frame);
    document.body.appendChild(btn);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount);
  else mount();
})();
