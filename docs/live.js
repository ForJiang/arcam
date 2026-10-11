// 实时相机层 —— index.html:3899-4037 的浏览器侧移植（FEATURE-SPEC §4）。
// docs（Pages）版：与 webui 版逐字同源（仅本行抬头注释不同）；快门帧由调用方
// 直接收（app.js 的 bindLive：arcamLive.shoot() → toBlob → setPhoto(File)），
// 中间没有 window.arcamPagesSetPhoto 之类的钩子。
// 摄像头 / 陀螺仪 / 快门 / 变焦；桌面端无传感器时保留手拖参数。
(function () {
  "use strict";

  var live = {
    active: false,
    stream: null,
    video: null,
    track: null,
    //: 传感器平滑系数（onOrient 的低通；原版同款 0.2 权重）
    SMOOTH: 0.2,
    //: 软转屏阈值：系统 angle=0 且 |roll|>45 → body rotate(±90)（syncTurn）
    TURN_THRESHOLD: 45,
    turn: 0,
    onFrame: null,       // 每帧回调（HUD 重绘用）
    onOrient: null       // 姿态回调（pitch/roll 已平滑）
  };

  function $(id) { return document.getElementById(id); }

  function setStatus(msg, isError) {
    var el = $("status-msg");
    if (el) {
      el.textContent = msg;
      el.classList.toggle("error", !!isError);
    }
  }

  // ── 启动：iOS 的 requestPermission 必须第一个 await（手势上下文）──
  async function start() {
    if (live.active) return;
    var video = $("cam-video");
    if (!video) return;
    if (!window.isSecureContext) {
      setStatus(liveT("gate.failInsecure") || "不是安全上下文：摄像头和动作传感器只在 https:// 或 http://localhost 下可用", true);
      return;
    }
    try {
      // ⚠ 运动权限必须第一个要，而且要在用户手势里同步调 —— 中间夹一次 await
      //   （下面的 getUserMedia）手势上下文就没了，再调直接被拒（index.html:3902-3909）。
      if (typeof DeviceOrientationEvent !== "undefined" &&
          typeof DeviceOrientationEvent.requestPermission === "function") {
        await DeviceOrientationEvent.requestPermission();
      }
      var stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: { ideal: "environment" }, width: { ideal: 1920 } },
        audio: false
      });
      live.stream = stream;
      live.track = stream.getVideoTracks()[0];
      video.srcObject = stream;
      await video.play();
      live.video = video;
      live.active = true;

      setupZoom(live.track);
      window.addEventListener("deviceorientation", onOrient, true);

      $("btn-live").textContent = liveT("btn.liveStop") || "关闭相机";
      $("live-row").hidden = false;
      video.hidden = false;
      $("preview-empty").hidden = true;
      setStatus(liveT("gate.opening") || "相机已开启");
      fit();
      window.addEventListener("resize", fit);
      if (screen.orientation && screen.orientation.addEventListener) {
        screen.orientation.addEventListener("change", function () {
          setTimeout(function () { fit(); }, 60);
        });
      }
      // 三时机补播：页面被挂起（分享面板/切后台）后浏览器会停掉 video 解码
      document.addEventListener("visibilitychange", resumeCam);
      window.addEventListener("focus", resumeCam);
      window.addEventListener("pageshow", resumeCam);
    } catch (err) {
      var msg = (err && err.name === "NotAllowedError")
        ? (liveT("gate.failDenied") || "权限被拒：请在浏览器地址栏允许摄像头后刷新")
        : (liveT("gate.fail") || "打开相机失败") + "：" + ((err && err.name) || "?") + " " + ((err && err.message) || "");
      setStatus(msg, true);
    }
  }

  function liveT(key) {
    return window.arcamT ? window.arcamT(key) : null;
  }

  function stop() {
    if (!live.active) return;
    if (live.stream) live.stream.getTracks().forEach(function (t) { t.stop(); });
    live.stream = null;
    live.track = null;
    live.active = false;
    var video = $("cam-video");
    if (video) { video.srcObject = null; video.hidden = true; }
    window.removeEventListener("deviceorientation", onOrient, true);
    document.removeEventListener("visibilitychange", resumeCam);
    window.removeEventListener("focus", resumeCam);
    window.removeEventListener("pageshow", resumeCam);
    window.removeEventListener("resize", fit);
    $("btn-live").textContent = liveT("btn.live") || "开启相机";
    $("live-row").hidden = true;
    $("zoom-cell").hidden = true;
    setStatus("就绪");
  }

  // ── cover 铺满物理视口（index.html:3923-3941 的 fit）──
  function fit() {
    var video = live.video;
    if (!video || !video.videoWidth) return;
    var w = video.videoWidth, h = video.videoHeight;
    var sc = Math.max(window.innerWidth / w, window.innerHeight / h);
    video.style.width = (w * sc) + "px";
    video.style.height = (h * sc) + "px";
    var cv = $("preview-canvas");
    if (cv) { cv.width = w; cv.height = h; }
    var hud = $("hud-canvas");
    if (hud) { hud.width = w; hud.height = h; }
  }

  // ── 陀螺仪：beta/gamma → pitch/roll（index.html:1059-1100 onOrient）──
  //: 约定（原版逐字）：pitch = beta（低头为正，与 S.pitch 同向）；
  //: roll = gamma 顺时针为正，再按 screen.orientation.angle 补偿。
  var smoothed = { pitch: 0, roll: 0 };

  function onOrient(ev) {
    if (ev.beta === null || ev.gamma === null) return;
    var ang = (screen.orientation && screen.orientation.angle) || 0;
    var pitch = ev.beta;
    var roll = ev.gamma;
    // 系统转屏补偿：angle=90/270/-90 时把 roll 归零（画面已被系统转正）
    if (ang === 90) roll -= 90;
    else if (ang === 270 || ang === -90) roll += 90;
    else if (ang === 180) { pitch = -pitch; roll = -roll; }
    // 低通平滑（防抖；原版同款权重）
    smoothed.pitch += (pitch - smoothed.pitch) * live.SMOOTH;
    smoothed.roll += (roll - smoothed.roll) * live.SMOOTH;
    if (live.onOrient) live.onOrient(smoothed.pitch, smoothed.roll);
    syncTurn(smoothed.roll);
  }

  // ── 软转屏：系统没转、但 roll 过大 → 把自己转过来（index.html:524-588）──
  function syncTurn(roll) {
    var ang = (screen.orientation && screen.orientation.angle) || 0;
    var want = (ang === 0 && Math.abs(roll) > live.TURN_THRESHOLD) ? (roll > 0 ? 90 : -90) : 0;
    if (want === live.turn) return;
    live.turn = want;
    var b = document.body;
    if (!want) {
      b.style.cssText = "";
    } else {
      b.style.position = "fixed";
      b.style.left = "50%"; b.style.top = "50%";
      b.style.width = window.innerHeight + "px";
      b.style.height = window.innerWidth + "px";
      b.style.transformOrigin = "center center";
      // ⚠ rotate(+TURN) 不是 -TURN：CSS 正角顺时针，只有 +TURN 把底边送到
      //    roll=+90 时该在的那一侧（-TURN 会整整差 180°，原版实测过）。
      b.style.transform = "translate(-50%,-50%) rotate(" + want + "deg)";
      // 世界层（video+canvas）反着转回去，抵消 body 这一下
      var video = $("cam-video");
      if (video) video.style.transform = "translate(-50%,-50%) rotate(" + (-want) + "deg)";
    }
    fit();
  }

  // ── 变焦：caps.zoom 缺失（iOS）→ 整格消失（index.html:3855-3896）──
  function setupZoom(track) {
    if (!track || typeof track.getCapabilities !== "function") return;
    var z = null;
    try { z = (track.getCapabilities() || {}).zoom; } catch (e) { return; }
    if (!z || typeof z.min !== "number" || typeof z.max !== "number" || z.max <= z.min) return;
    var el = $("zoom-rng"), out = $("zoom-val"), cell = $("zoom-cell");
    if (!el || !out || !cell) return;
    el.min = z.min;
    el.max = z.max;
    el.step = (typeof z.step === "number" && z.step > 0) ? z.step : (z.max - z.min) / 100;
    var cur = z.min;
    try {
      var s = track.getSettings();
      if (s && typeof s.zoom === "number") cur = s.zoom;
    } catch (e) {}
    el.value = cur;
    out.textContent = cur.toFixed(1) + "×";
    cell.hidden = false;
    el.addEventListener("input", function () {
      var val = parseFloat(el.value);
      out.textContent = val.toFixed(1) + "×";
      try {
        var p = track.applyConstraints({ advanced: [{ zoom: val }] });
        if (p && typeof p.catch === "function") {
          p.catch(function () { zoomFail(el, out, cell); });
        }
      } catch (e) { zoomFail(el, out, cell); }
    });
  }

  function zoomFail(el, out, cell) {
    if (el.disabled) return;
    el.disabled = true;
    cell.hidden = true;                       // 这颗镜头调不了变焦 → 整格消失
  }

  // ── 快门：当前视频帧 → 照片（index.html:3469-3493 buildShotCanvas）──
  function shoot() {
    var video = live.video;
    if (!video || !video.videoWidth) return null;
    var cv = document.createElement("canvas");
    cv.width = video.videoWidth;
    cv.height = video.videoHeight;
    cv.getContext("2d").drawImage(video, 0, 0);
    return cv;
  }

  function resumeCam() {
    if (document.hidden || !live.active) return;
    if (live.video && live.video.paused) {
      var p = live.video.play();
      if (p && p.catch) p.catch(function () {});
    }
    if (live.onFrame) live.onFrame();
  }

  live.start = start;
  live.stop = stop;
  live.shoot = shoot;
  live.fit = fit;
  window.arcamLive = live;
})();
