/* VS Course — one-click ASC grabber.
 *
 * Paste this WHOLE file into the DevTools Console (F12) of your logged-in ASC
 * tab showing the "Running Courses" department list (Academic -> All About
 * Courses -> Running Courses, with your year + semester picked). If the console
 * refuses to paste, type "allow pasting" first and retry.
 *
 * It fetches every department's running-courses page plus every course's
 * detail page over your existing session — same-origin GETs only, exactly the
 * links the page itself shows; your password is never seen or stored — then
 * downloads ONE asc-courses-<year>-sem<n>.json. Drop that file onto VS Course.
 */
(async () => {
  "use strict";

  // Collect every same-origin frame document (ASC is a frameset).
  const docs = [];
  (function walk(w) {
    try { if (w.document) docs.push(w.document); } catch (e) { return; }
    for (let i = 0; i < w.frames.length; i++) walk(w.frames[i]);
  })(window);

  // The department list lives in one of the frames as plain GET links.
  let deptLinks = [], hostDoc = null;
  for (const d of docs) {
    const as = [...d.querySelectorAll('a[href*="RunningCourses.jsp?deptcd="]')];
    if (as.length && !hostDoc) hostDoc = d;
    deptLinks.push(...as.map(a => a.href));
  }
  deptLinks = [...new Set(deptLinks)];
  if (!deptLinks.length) {
    alert("No department links found.\nOpen: Academic \u2192 All About Courses \u2192 " +
          "Running Courses, pick the year + semester, then run this again.");
    return;
  }

  // Progress toast. The top page is a frameset (renders no children), so the
  // toast goes into the frame that holds the department list.
  const ui = hostDoc.createElement("div");
  ui.style.cssText = "position:fixed;top:12px;right:12px;z-index:2147483647;" +
    "background:#1f2430;color:#fff;font:13px/1.5 sans-serif;padding:10px 14px;" +
    "border-radius:9px;box-shadow:0 6px 20px rgba(0,0,0,.35);max-width:340px";
  hostDoc.body.appendChild(ui);
  const status = t => { ui.textContent = "VS Course grabber \u2014 " + t; console.log("[grab]", t); };

  // ASC's JSPs are old and often omit the charset header; r.text() would then
  // assume UTF-8 and turn every ’ or – in a course name into \uFFFD ("Bachelor�s").
  // Decode the raw bytes: declared charset > strict UTF-8 > windows-1252.
  const fetchText = async url => {
    const r = await fetch(url, { credentials: "include" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const buf = await r.arrayBuffer();
    let cs = (/charset=([\w-]+)/i.exec(r.headers.get("content-type") || "") || [])[1];
    if (!cs) {  // sniff a <meta charset=...> in the head (ASCII-safe peek)
      const head = new TextDecoder("latin1").decode(buf.slice(0, 2048));
      cs = (/<meta[^>]+charset=["']?([\w-]+)/i.exec(head) || [])[1];
    }
    if (cs) { try { return new TextDecoder(cs).decode(buf); } catch (e) { /* unknown label */ } }
    try { return new TextDecoder("utf-8", { fatal: true }).decode(buf); }
    catch (e) { return new TextDecoder("windows-1252").decode(buf); }
  };

  const first = new URL(deptLinks[0]);
  const year = first.searchParams.get("year") || "";
  const sem = first.searchParams.get("semester") || "";
  const bundle = { kind: "asc-bundle", version: 1, grabbed_at: new Date().toISOString(),
                   year, semester: sem, source: location.hostname,
                   pages: [], details: {}, errors: [] };

  // 1) Every department's running-courses page.
  const codes = new Set();
  for (let i = 0; i < deptLinks.length; i++) {
    const url = deptLinks[i];
    status(`departments ${i + 1}/${deptLinks.length}\u2026`);
    try {
      const html = await fetchText(url);
      bundle.pages.push({ dept: new URL(url).searchParams.get("deptcd") || "", url, html });
      // NOTE: live hrefs carry RAW SPACES in the code ("ccd=CS 101&view="), so
      // the value must be read up to the next & / quote, not the next space.
      for (const m of html.matchAll(/crsedetail\.jsp\?ccd=([^"'&<>]+)/g)) {
        let code = m[1];
        try { code = decodeURIComponent(code); } catch (e) { /* keep raw */ }
        code = code.replace(/\+/g, " ").replace(/\s+/g, " ").trim();
        // real course codes contain digits; digit-less matches are bare dept
        // links (e.g. "ccd=CS"), whose detail page just returns HTTP 500
        if (/\d/.test(code)) codes.add(code);
      }
    } catch (e) { bundle.errors.push({ url, error: String(e) }); }
  }

  // 2) Every course's detail page (official credits, half-sem flag, description).
  const queue = [...codes], total = queue.length;
  let done = 0;
  const detailUrl = c => first.origin +
    "/academic/CourseRegistration/Common/crsedetail.jsp?ccd=" + encodeURIComponent(c) + "&view=";
  async function worker() {
    for (let c; (c = queue.shift()) !== undefined;) {
      const url = detailUrl(c);
      try { bundle.details[c] = { url, html: await fetchText(url) }; }
      catch (e) { bundle.errors.push({ url, error: String(e) }); }
      done++;
      if (done % 10 === 0 || done === total) status(`course details ${done}/${total}\u2026`);
    }
  }
  await Promise.all(Array.from({ length: 6 }, () => worker()));

  // 3) Download the single bundle file.
  const name = `asc-courses-${year || "unknown"}-sem${sem || "x"}.json`;
  const a = hostDoc.createElement("a");
  a.href = URL.createObjectURL(new Blob([JSON.stringify(bundle)], { type: "application/json" }));
  a.download = name;
  hostDoc.body.appendChild(a); a.click(); a.remove();
  status(`done \u2014 ${bundle.pages.length} dept pages, ${Object.keys(bundle.details).length} course details` +
         (bundle.errors.length ? `, ${bundle.errors.length} fetches failed (kept going)` : "") +
         `. Saved ${name} \u2014 drop it onto VS Course.`);
  if (bundle.errors.length)
    console.warn("[grab] failed fetches (also recorded in the bundle):", bundle.errors.slice(0, 10));
  setTimeout(() => ui.remove(), 15000);
})();
