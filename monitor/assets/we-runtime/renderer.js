/* Monitor adapter, informed by dsh-wallpaper-engine's MIT renderer.
 * Scene success means WebWallGL's load promise reached its first frame, never a
 * poster. Web documents run in an opaque-origin allow-scripts-only iframe.
 */
(() => {
  "use strict";
  const host = document.getElementById("wp");
  const query = new URLSearchParams(location.search);
  const assetBase = new URL("../asset/", location.href).href;
  const kind = query.get("kind");
  const entry = query.get("entry");
  let instance = null, frame = null, paused = false, dead = false;
  window.__monitorState = {status: "loading", error: ""};
  function fail(error) {
    if (dead) return;
    window.__monitorState = {status: "failed", error: String(error?.message || error).slice(0, 600)};
    try { instance?.destroy(); } catch (_) {}
    instance = null;
    frame?.remove(); frame = null;
  }
  window.__monitor = {
    pause(value) {
      paused = !!value;
      if (paused) instance?.pause(); else instance?.resume();
      // Native page lifecycle freezes Web timers too; this message also supports
      // Wallpaper Engine's standard pause listener for cooperative web projects.
      frame?.contentWindow?.postMessage({__monitorWallpaper: true, op: "pause", paused}, "*");
    },
    style(style) {
      const fits = ["cover", "contain", "stretch"];
      if (!fits.includes(style.fit)) throw new Error("Unsupported fit");
      instance?.setFit(style.fit);
      host.style.filter = `brightness(${style.brightness}) contrast(${style.contrast}) saturate(${style.saturation}) blur(${style.blur}px)`;
      host.style.transform = `translate(${style.x - 50}%, ${style.y - 50}%) scale(${style.scale / 100})`;
      // Web is a viewport (its own canvas/layout fills it); cover/contain use
      // the viewport aspect, while scene/image layers use setFit above.
      if (frame) frame.style.objectFit = style.fit === "stretch" ? "fill" : style.fit;
    },
    dispose() {
      dead = true;
      try { instance?.destroy({releasePkgCache: true}); } catch (_) {}
      instance = null; frame?.remove(); frame = null;
    }
  };
  window.addEventListener("message", event => {
    if (frame && event.source === frame.contentWindow && event.data?.__monitorWallpaperError) {
      fail(new Error("网页脚本错误：" + String(event.data.error || "未知错误")));
    }
  });
  window.addEventListener("pagehide", () => window.__monitor.dispose());
  window.addEventListener("error", event => fail(event.error || event.message));
  document.addEventListener("contextmenu", event => event.preventDefault());
  async function load() {
    if (kind === "live_web") {
      if (!entry) throw new Error("Missing local HTML entry");
      frame = document.createElement("iframe");
      frame.setAttribute("sandbox", "allow-scripts");
      frame.setAttribute("allow", "camera 'none'; microphone 'none'; geolocation 'none'; clipboard-read 'none'; clipboard-write 'none'");
      frame.referrerPolicy = "no-referrer";
      frame.addEventListener("error", () => fail(new Error("本地网页加载失败")));
      frame.addEventListener("load", async () => {
        try {
          const project = await fetch(assetBase + "project.json").then(response => response.json());
          frame?.contentWindow?.postMessage({__monitorWallpaper: true, op: "properties",
            properties: project.general?.properties || {}}, "*");
          frame?.contentWindow?.postMessage({__monitorWallpaper: true, op: "pause", paused}, "*");
        } catch (_) {}
        if (!dead && window.__monitorState.status !== "failed") {
          window.__monitorState = {status: "ready", error: ""};
        }
      });
      frame.src = assetBase + entry.split("/").map(encodeURIComponent).join("/");
      host.appendChild(frame);
    } else if (kind === "live_scene") {
      if (!globalThis.WebWallGL) {
        await new Promise((resolve, reject) => {
          const script = document.createElement("script");
          script.src = new URL("./webwallgl.global.min.js", location.href).href;
          script.onload = resolve;
          script.onerror = () => reject(new Error("Offline scene engine failed to load"));
          document.head.appendChild(script);
        });
      }
      if (dead) return;
      if (!globalThis.WebWallGL) throw new Error("Missing offline WebWallGL bundle");
      const {createScene, httpSource} = globalThis.WebWallGL;
      instance = createScene(host, {fit: "cover", fps: 30, volume: 0, renderDpr: 1,
        onDiagnostic(message, level) { if (level === "error") console.warn(String(message)); }});
      instance.on("error", fail);
      await instance.load(httpSource(assetBase.replace(/\/$/, "")));
      if (dead || window.__monitorState.status === "failed") return;
      instance.setVolume(0);
      if (paused) instance.pause();
      window.__monitorState = {status: "ready", error: ""};
    } else throw new Error("Unsupported wallpaper kind");
  }
  load().catch(fail);
})();
