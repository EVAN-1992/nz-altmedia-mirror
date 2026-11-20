(function () {
  "use strict";
  var meta = {};
  try { meta = JSON.parse(document.getElementById("brief-meta").textContent); } catch (e) {}
  var DATE = meta.date || "unknown";
  var LS_KEY = "brief-votes-" + DATE;
  var items = [].slice.call(document.querySelectorAll(".item"));
  var claudeUse = window.claude && typeof window.claude.use === "function" ? window.claude.use.bind(window.claude) : null;

  /* ---------- toast ---------- */
  function toast(msg) {
    var t = document.getElementById("toast");
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(toast._t);
    toast._t = setTimeout(function () { t.classList.remove("show"); }, 2800);
  }

  /* ---------- votes: saved to the artifact's store so the next run reads them ---------- */
  var votes = {};
  try { votes = JSON.parse(localStorage.getItem(LS_KEY) || "{}") || {}; } catch (e) { votes = {}; }
  var db = null;

  function paint(id) {
    var el = document.getElementById(id);
    if (!el) return;
    el.querySelectorAll(".vote").forEach(function (b) {
      b.setAttribute("aria-pressed", votes[id] === b.dataset.vote ? "true" : "false");
    });
  }
  function saveLocal() { try { localStorage.setItem(LS_KEY, JSON.stringify(votes)); } catch (e) {} }

  function saveRemote(el, v) {
    if (!db) return;
    var ref = db.collection("votes").doc(DATE + "__" + el.id);
    var p = v
      ? ref.set({
          date: DATE, item: el.id, vote: v,
          section: el.dataset.section || "", story: el.dataset.story || "",
          headline: el.dataset.headline || "", ts: new Date().toISOString()
        })
      : ref.delete();
    p.then(function () { toast(v ? "Saved. Tomorrow's brief will take this into account" : "Vote removed"); })
     .catch(function () { toast("Vote kept on this device only. Use Feedback to pass it on"); });
  }

  items.forEach(function (el) {
    paint(el.id);
    el.querySelectorAll(".vote").forEach(function (b) {
      b.addEventListener("click", function () {
        var v = b.dataset.vote;
        if (votes[el.id] === v) delete votes[el.id]; else votes[el.id] = v;
        saveLocal();
        paint(el.id);
        saveRemote(el, votes[el.id] || null);
        if (!db) toast("Vote kept on this device. Use Feedback to pass it on");
      });
    });
  });

  if (claudeUse) {
    claudeUse("db").then(function (ns) {
      if (!ns) return;
      db = ns;
      db.collection("votes").where("date", "==", DATE).get().then(function (snap) {
        snap.docs.forEach(function (d) {
          var x = d.data() || {};
          if (x.item && x.vote && !votes[x.item]) { votes[x.item] = x.vote; paint(x.item); }
        });
        saveLocal();
      }).catch(function () {});
    }).catch(function () {});
  }

  /* ---------- copy feedback (works everywhere, including outside the viewer) ---------- */
  document.getElementById("fbBtn").addEventListener("click", function () {
    var up = [], down = [];
    items.forEach(function (el) {
      var line = "[" + el.id + "] " + (el.dataset.headline || "");
      if (votes[el.id] === "up") up.push(line);
      if (votes[el.id] === "down") down.push(line);
    });
    var text = ["Daily brief feedback for " + DATE, "", "More like this:"]
      .concat(up.length ? up : ["(none)"], ["", "Less like this:"], down.length ? down : ["(none)"])
      .join("\n");
    function fallback() {
      var ta = document.getElementById("fb-text");
      ta.hidden = false; ta.value = text; ta.focus(); ta.select();
      toast("Couldn't copy automatically. The text is selected below");
    }
    try {
      navigator.clipboard.writeText(text).then(function () { toast("Feedback copied"); }, fallback);
    } catch (e) { fallback(); }
  });

  /* ---------- listen: Claude voice where offered, else the browser's ---------- */
  var listenBtn = document.getElementById("listenBtn");
  var listenLbl = document.getElementById("listenLbl");
  var tts = null, synth = window.speechSynthesis || null, playing = false, idx = -1;

  function mark(id) {
    items.forEach(function (el) { el.classList.toggle("reading", el.id === id); });
    var el = id && document.getElementById(id);
    if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
  }
  function setPlaying(on) {
    playing = on;
    listenBtn.setAttribute("aria-pressed", on ? "true" : "false");
    listenLbl.textContent = on ? "Stop" : "Listen";
    if (!on) mark(null);
  }
  function textOf(el) {
    var h = el.querySelector("h3"), w = el.querySelector(".whatsnew"), s = el.querySelector(".summary");
    return [h && h.textContent, w && w.textContent, s && s.textContent].filter(Boolean).join(". ").slice(0, 1990);
  }

  if (claudeUse) {
    claudeUse("tts").then(function (ns) {
      if (!ns) return;
      tts = ns;
      try {
        tts.onLine(function (e) { if (playing && e && e.id) mark(e.id); });
        tts.onState(function (e) { if (e && e.state === "idle" && playing) setPlaying(false); });
      } catch (e) {}
    }).catch(function () {});
  }

  function browserNext() {
    idx++;
    if (!playing || idx >= items.length) { setPlaying(false); return; }
    var el = items[idx];
    mark(el.id);
    var u = new SpeechSynthesisUtterance(textOf(el));
    u.lang = "en-NZ";
    u.onend = function () { if (playing) browserNext(); };
    u.onerror = u.onend;
    synth.speak(u);
  }

  if (!synth && !claudeUse) listenBtn.hidden = true;
  listenBtn.addEventListener("click", function () {
    if (playing) {
      setPlaying(false);
      if (tts) { tts.stop().catch(function () {}); }
      if (synth) synth.cancel();
      return;
    }
    if (tts) {
      setPlaying(true);
      var lines = items.map(function (el) { return { text: textOf(el), id: el.id }; });
      tts.speak(lines, { language: "en" }).then(function () { setPlaying(false); }, function (err) {
        setPlaying(false);
        if (err && err.code === "not_granted") { tts = null; toast("Reading aloud is off for this page. Using the browser voice next time"); }
        else if (err && err.code === "rate_limited") toast("Reading aloud is busy. Try again in a minute");
      });
      return;
    }
    if (synth) { setPlaying(true); idx = -1; synth.cancel(); browserNext(); return; }
    toast("Reading aloud isn't available in this browser");
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && playing) listenBtn.click(); });

  /* ---------- section nav highlights the section in view ---------- */
  var links = [].slice.call(document.querySelectorAll(".nav a"));
  if ("IntersectionObserver" in window && links.length) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (!en.isIntersecting) return;
        links.forEach(function (a) { a.classList.toggle("on", a.getAttribute("href") === "#" + en.target.id); });
      });
    }, { rootMargin: "-30% 0px -60% 0px" });
    document.querySelectorAll(".sec").forEach(function (s) { io.observe(s); });
  }
})();
