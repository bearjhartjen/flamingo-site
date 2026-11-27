(function () {
  "use strict";

  // ---- Theme ---------------------------------------------------------------
  // The visitor's choice follows them across pages. Only the storage calls
  // are guarded (private mode can throw); any real script error reaches the
  // console instead of being swallowed.
  var THEMES = ["classic", "parchment", "ink", "midnight"];
  var body = document.body;
  var defaultTheme = body.dataset.theme || "classic";

  function savedTheme() {
    try { return localStorage.getItem("fc-theme"); } catch (err) { return null; }
  }
  function saveTheme(name) {
    try { localStorage.setItem("fc-theme", name); } catch (err) { /* not fatal */ }
  }

  var saved = savedTheme();
  if (THEMES.indexOf(saved) !== -1) { body.dataset.theme = saved; }

  var themeButton = document.getElementById("theme-toggle");
  function labelThemeButton() {
    if (themeButton) {
      themeButton.textContent = body.dataset.theme === "midnight" ? "Light mode" : "Dark mode";
    }
  }
  labelThemeButton();
  if (themeButton) {
    themeButton.addEventListener("click", function () {
      var next = body.dataset.theme === "midnight" ? defaultTheme : "midnight";
      body.dataset.theme = next;
      saveTheme(next);
      labelThemeButton();
    });
  }

  // ---- Arrival flash -------------------------------------------------------
  // The burst sets fcWarp just before it navigates; the next page plays one
  // soft ink flash so the arrival feels joined to the click.
  var warped = false;
  try {
    warped = sessionStorage.getItem("fcWarp") === "1";
    if (warped) { sessionStorage.removeItem("fcWarp"); }
  } catch (err) { /* storage unavailable: skip the flash */ }
  if (warped) {
    var arrive = document.createElement("div");
    arrive.id = "arriveflash";
    document.body.appendChild(arrive);
    document.body.classList.add("arrive");
    setTimeout(function () {
      document.body.classList.remove("arrive");
      if (arrive.parentNode) { arrive.parentNode.removeChild(arrive); }
    }, 700);
  }

  // ---- Live GitHub repository strips --------------------------------------
  // The build writes a .gh-repo shell wherever a page uses the marker
  // [github-stats:owner/repo]. The stats come from GitHub's public API here,
  // in the browser: no token, no server, no build-time network. Responses
  // are cached for half an hour so a reader moving between pages does not
  // spend the anonymous rate limit. All repository text is written with
  // textContent, never innerHTML.
  (function () {
    var strips = document.querySelectorAll("[data-gh-repo]");
    if (!strips.length) { return; }

    var API = "https://api.github.com";
    var TTL = 30 * 60 * 1000;

    function cacheKey(repo) { return "fc-gh:" + repo; }
    function readCache(repo) {
      try {
        var raw = localStorage.getItem(cacheKey(repo));
        if (!raw) { return null; }
        var doc = JSON.parse(raw);
        if (!doc || typeof doc.t !== "number" || !doc.data) { return null; }
        return doc;
      } catch (err) { return null; }
    }
    function writeCache(repo, data) {
      try {
        localStorage.setItem(cacheKey(repo), JSON.stringify({ t: Date.now(), data: data }));
      } catch (err) { /* private mode: live stats still work this visit */ }
    }
    function getJSON(url) {
      return fetch(url, { headers: { "Accept": "application/vnd.github+json" } })
        .then(function (response) {
          if (response.status === 404) { return null; }
          if (!response.ok) { throw new Error("GitHub " + response.status); }
          return response.json();
        });
    }
    function setText(root, name, text) {
      var el = root.querySelector('[data-gh="' + name + '"]');
      if (el) { el.textContent = text; }
    }
    function setTitle(root, name, text) {
      var el = root.querySelector('[data-gh="' + name + '"]');
      if (el) { el.title = text || ""; }
    }
    function relative(iso) {
      if (!iso) { return ""; }
      var then = Date.parse(iso);
      if (isNaN(then)) { return ""; }
      var seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
      if (seconds < 60) { return "just now"; }
      var minutes = Math.round(seconds / 60);
      if (minutes < 60) { return minutes + " min ago"; }
      var hours = Math.round(minutes / 60);
      if (hours < 24) { return hours + (hours === 1 ? " hour ago" : " hours ago"); }
      var days = Math.round(hours / 24);
      if (days < 30) { return days + (days === 1 ? " day ago" : " days ago"); }
      try {
        return new Date(then).toLocaleDateString(undefined, {
          month: "short", day: "numeric", year: "numeric"
        });
      } catch (err) { return ""; }
    }
    function shortLine(text, limit) {
      var line = String(text || "").split("\n")[0].trim();
      return line.length > limit ? line.slice(0, limit - 1).trim() + "\u2026" : line;
    }
    function safeGithub(url) {
      return /^https:\/\/github\.com\//.test(String(url || "")) ? url : "";
    }
    function paint(root, doc) {
      var data = doc.data || {};
      var repoData = data.repo || {};
      var commit = data.commits && data.commits[0];
      var release = data.release;
      var stars = typeof repoData.stargazers_count === "number" ? repoData.stargazers_count : null;
      var issues = typeof repoData.open_issues_count === "number" ? repoData.open_issues_count : null;

      setText(root, "stars", stars === null ? "\u2014" : String(stars));
      setText(root, "stars-unit", stars === 1 ? "star" : "stars");
      setText(root, "issues", issues === null ? "\u2014" : String(issues));
      setText(root, "issues-unit", issues === 1 ? "open issue" : "open issues");

      if (commit) {
        var commitData = commit.commit || {};
        var author = commitData.author || commitData.committer || {};
        var message = shortLine(commitData.message, 64) || "No commit message";
        setText(root, "commit", message);
        setTitle(root, "commit", String(commitData.message || "").split("\n")[0].trim());
        setText(root, "commit-when", relative(author.date));
        var commitLink = root.querySelector('[data-gh="commit-link"]');
        if (commitLink) {
          var commitHref = safeGithub(commit.html_url);
          if (commitHref) { commitLink.href = commitHref; }
        }
      } else {
        setText(root, "commit", "No commits yet");
        setText(root, "commit-when", "");
      }

      if (release) {
        var releaseName = shortLine(release.tag_name || release.name, 32) || "Latest release";
        setText(root, "release", releaseName);
        setTitle(root, "release", release.tag_name || release.name || "");
        setText(root, "release-when", relative(release.published_at));
        var releaseLink = root.querySelector('[data-gh="release-link"]');
        if (releaseLink) {
          var releaseHref = safeGithub(release.html_url);
          if (releaseHref) { releaseLink.href = releaseHref; }
        }
      } else {
        setText(root, "release", "No releases yet");
        setText(root, "release-when", "");
      }
      root.dataset.state = "ready";
    }
    function fallback(root) {
      root.dataset.state = "error";
      setText(root, "commit", "Live stats unavailable");
      setText(root, "commit-when", "");
      setText(root, "release", "View on GitHub");
      setText(root, "release-when", "");
    }
    function load(root) {
      var repo = root.getAttribute("data-gh-repo") || "";
      if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo)) { return; }
      if (!window.fetch) { fallback(root); return; }
      root.dataset.state = "loading";
      var cached = readCache(repo);
      if (cached) { paint(root, cached); }
      if (cached && Date.now() - cached.t < TTL) { return; }

      var base = API + "/repos/" + repo;
      Promise.all([
        getJSON(base),
        getJSON(base + "/commits?per_page=1"),
        getJSON(base + "/releases?per_page=1")
      ]).then(function (parts) {
        if (!parts[0]) { throw new Error("repository not found"); }
        var next = {
          repo: parts[0],
          commits: parts[1] || [],
          release: (parts[2] && parts[2][0]) || null
        };
        writeCache(repo, next);
        paint(root, { t: Date.now(), data: next });
      }).catch(function () {
        if (!cached) { fallback(root); }
      });
    }
    for (var i = 0; i < strips.length; i++) { load(strips[i]); }
  })();

  // ---- Star / Feedback burst -----------------------------------------------
  var badge = document.querySelector("a.star-badge, a.gem-badge");
  if (!badge || !badge.getAttribute("href")) { return; }

  var reduceMotion = window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  badge.addEventListener("click", function (event) {
    var href = badge.getAttribute("href");
    if (!href || href.charAt(0) === "#") { return; }
    event.preventDefault();
    if (reduceMotion) { window.location.href = href; return; }
    if (badge.classList.contains("warp")) { return; }

    var rect = badge.getBoundingClientRect();
    var x = rect.left + rect.width / 2;
    var y = rect.top + rect.height / 2;

    try { sessionStorage.setItem("fcWarp", "1"); } catch (err) { /* flash optional */ }

    badge.classList.add("warp");
    document.body.classList.add("warping");

    var left = false;
    function go() {
      if (left) { return; }
      left = true;
      window.location.href = href;
    }
    setTimeout(go, 560);
    setTimeout(go, 1100);
    setTimeout(function () {
      document.body.classList.remove("warping");
      badge.classList.remove("warp");
      var layer = document.getElementById("fx");
      if (layer && layer.parentNode) { layer.parentNode.removeChild(layer); }
    }, 2200);

    try {
      drawBurst(x, y);
    } catch (err) {
      // The burst is decoration; the navigation timers above still run.
      console.error("star burst failed", err);
    }
  });

  function drawBurst(x, y) {
    var layer = document.createElement("div");
    layer.id = "fx";
    var flash = document.createElement("div");
    flash.className = "comicflash";
    layer.appendChild(flash);
    var canvas = document.createElement("canvas");
    layer.appendChild(canvas);
    document.body.appendChild(layer);

    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    var W = window.innerWidth;
    var H = window.innerHeight;
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    var ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);

    var count = W < 700 ? 18 : 30;
    var shards = [];
    for (var i = 0; i < count; i++) {
      var angle = Math.random() * Math.PI * 2;
      var speed = 3 + Math.random() * 6;
      shards.push({
        x: x, y: y,
        vx: Math.cos(angle) * speed,
        vy: Math.sin(angle) * speed,
        r: 4 + Math.random() * 7,
        rot: Math.random() * Math.PI,
        spin: (Math.random() - 0.5) * 0.4,
        age: 0,
        life: 26 + Math.random() * 22,
        ink: Math.random() < 0.5
      });
    }

    var rayCount = W < 700 ? 12 : 18;
    var raySeeds = [];
    for (var j = 0; j < rayCount; j++) { raySeeds.push(0.6 + Math.random() * 0.5); }

    var wax = { age: 0, life: 24 };
    var started = performance.now();
    requestAnimationFrame(frame);

    function frame() {
      var alive = false;
      ctx.clearRect(0, 0, W, H);

      // The sealed wax burst: a jagged white star with black-and-white rays.
      if (wax.age <= wax.life) {
        wax.age++;
        alive = true;
        var progress = Math.min(1, wax.age / wax.life);
        var radius = Math.min(W, H) * 0.08 + progress * 44;
        ctx.globalAlpha = 1 - progress * 0.3;
        ctx.fillStyle = "#fffdf7";
        ctx.strokeStyle = "#111";
        ctx.lineWidth = 5;
        ctx.beginPath();
        for (var k = 0; k < 16; k++) {
          var a = k * Math.PI / 8;
          var rr = radius * (1 + 0.22 * Math.sin(a * 3 + wax.age * 0.5));
          var px = x + Math.cos(a) * rr;
          var py = y + Math.sin(a) * rr;
          if (k) { ctx.lineTo(px, py); } else { ctx.moveTo(px, py); }
        }
        ctx.closePath();
        ctx.fill();
        ctx.stroke();
        for (var m = 0; m < rayCount; m++) {
          var ra = m * Math.PI * 2 / rayCount;
          var length = (0.3 + 0.16 * raySeeds[m]) * Math.max(W, H) * progress;
          ctx.beginPath();
          ctx.moveTo(x + Math.cos(ra) * radius * 0.7, y + Math.sin(ra) * radius * 0.7);
          ctx.lineTo(x + Math.cos(ra) * length, y + Math.sin(ra) * length);
          ctx.strokeStyle = m % 2 ? "#111" : "#fffdf7";
          ctx.lineWidth = m % 2 ? 9 : 4;
          ctx.stroke();
        }
        ctx.globalAlpha = 1;
      }

      // Ink shards, half filled and half outlined, flying out from the click.
      for (var s = 0; s < shards.length; s++) {
        var shard = shards[s];
        shard.age++;
        if (shard.age > shard.life) { continue; }
        alive = true;
        shard.x += shard.vx;
        shard.y += shard.vy;
        shard.vx *= 0.97;
        shard.vy *= 0.97;
        shard.rot += shard.spin;
        ctx.globalAlpha = Math.max(0, 1 - shard.age / shard.life);
        ctx.save();
        ctx.translate(shard.x, shard.y);
        ctx.rotate(shard.rot);
        ctx.beginPath();
        ctx.moveTo(0, -shard.r);
        ctx.lineTo(shard.r * 0.7, shard.r);
        ctx.lineTo(-shard.r * 0.7, shard.r);
        ctx.closePath();
        if (shard.ink) {
          ctx.fillStyle = "#111";
          ctx.fill();
        } else {
          ctx.fillStyle = "#fffdf7";
          ctx.fill();
          ctx.lineWidth = 2;
          ctx.strokeStyle = "#111";
          ctx.stroke();
        }
        ctx.restore();
      }

      ctx.globalAlpha = 1;
      if (alive && performance.now() - started < 1200) {
        requestAnimationFrame(frame);
      } else if (layer.parentNode) {
        layer.parentNode.removeChild(layer);
      }
    }
  }
})();
