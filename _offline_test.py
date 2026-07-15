"""Headless OFFLINE verification for the vendored-Pyodide timetable app.

Blocks ALL external http(s) (so the jsDelivr CDN is unreachable) -> if the page
boots, it MUST be from the local ./pyodide/ files. Then uploads BOTH ASC dumps
(CS + Math) and checks the units/plan/credits features:
  * MA 105 is grouped by division (4 rows, not ~40)
  * unscheduled bucket shows slotless courses (MA 593 / SI 593 / MAS801)
  * plan add/remove works; credits + weekly contact hours total up
  * clash detection flags two same-slot courses (CS 213 vs SI 423, slot 1)
  * manual credit override changes the plan total
  * CREDITS (division-invariant, hours-based C=2L+2T+P):
      MA 105 == 8 for every division, MA 419 == 8 (tutorial counted),
      CS 213 == 6 (~3h lecture), CS 293 == 3 (~3h lab),
      MAS801 == 4 (seminar override), MA 593 / CS 490 == 6 (R&D/project override)
  * hr/week values are whole integers; the L-T-P triple is shown
  * planning a unit that lives in Tutorials/extras or Unscheduled highlights it
    there immediately (extras re-render on the same reactive path as the plan)
  * Feature 1 — a restricted course exposes a non-empty reason string vs a
    mismatching profile ("Restricted — Open to … only. Your profile: …")
  * Feature 2 — the SEPARATE "When I'm busy" table (fixed weekday x time grid)
    shows red busy cells, a hatched CLASH cell for two overlapping planned units,
    and a red cell for a user-marked external commitment; it is NOT injected into
    the main variable-width grid, and that grid carries NO busy tint class
    (.cell.busy/.cell.free) — the busy table is the sole busy indicator
  * Feature 3 — confirming/pinning a core adds it to the plan, marks it core,
    counts in totals, and persists to localStorage
  * Feature 4 — timetable-share.html references the CDN + flips FORCE_CDN
    true, while the default timetable.html stays local (FORCE_CDN false)
  * Feature 5 — a grab.js BUNDLE (.json with dept pages + crsedetail pages)
    uploads like any file; official credits/L-T-P/half-sem/description attach
    to the courses, official credits take precedence over the estimate, the
    half-sem tag renders, and GRAB_JS is embedded for copy-to-clipboard

Usage: python _offline_test.py <url> <out_png>
"""
import os
import pathlib
import sys
from playwright.sync_api import sync_playwright

URL = sys.argv[1]
OUT_PNG = sys.argv[2] if len(sys.argv) > 2 else "preview9.png"
WEBAPP = pathlib.Path(__file__).resolve().parent


def check_share_build():
    """Static check (no browser): the share build hard-selects the CDN path and
    the default offline build does not — so the single share file needs no
    ./pyodide/ folder. Booting it needs internet, which this harness blocks."""
    off = (WEBAPP / "timetable.html").read_text(encoding="utf-8")
    shr = (WEBAPP / "timetable-share.html").read_text(encoding="utf-8")
    return {
        "offline_force_cdn_false": "FORCE_CDN = /*__FORCE_CDN__*/false" in off,
        "share_force_cdn_true": "FORCE_CDN = /*__FORCE_CDN__*/true" in shr,
        "share_has_cdn": "cdn.jsdelivr.net/pyodide" in shr,
    }
# Fixture ASC dumps + browser binary are machine-specific; override via env.
CS = os.environ.get("VSC_FIXTURE_CS",
                    "/home/bhavya/Downloads/Welcome to ASC ! (11_7_2026 12：10：14 pm).html")
MATH = os.environ.get("VSC_FIXTURE_MATH",
                      "/home/bhavya/Downloads/Welcome to ASC ! (11_7_2026 12：54：48 pm).html")
BROWSER = os.environ.get("VSC_BROWSER", "/usr/bin/brave-browser")

console_logs, blocked = [], []


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=BROWSER,
            headless=True,
            args=["--no-sandbox", "--disable-gpu", "--allow-file-access-from-files"],
        )
        page = browser.new_page()
        page.on("console", lambda m: console_logs.append(f"[{m.type}] {m.text}"))
        page.on("pageerror", lambda e: console_logs.append(f"[pageerror] {e}"))

        # Simulate offline: allow only localhost; abort everything external (CDN, PyPI).
        def gate(route):
            u = route.request.url
            if "localhost" in u or "127.0.0.1" in u:
                route.continue_()
            else:
                blocked.append(u)
                route.abort()
        page.route("http://**", gate)
        page.route("https://**", gate)

        print(f"== loading {URL}")
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)

        ok = True
        try:
            page.wait_for_function(
                "document.getElementById('parserStatus') && "
                "/Parser ready/.test(document.getElementById('parserStatus').textContent)",
                timeout=120000,
            )
            print("== parser READY (CDN blocked -> booted from LOCAL ./pyodide/)")
        except Exception as e:
            ok = False
            status = page.evaluate("document.getElementById('parserStatus')?.textContent")
            print(f"!! parser NOT ready: {e}\n   status='{status}'")

        info = {}
        if ok:
            print(f"== uploading BOTH dumps")
            page.set_input_files("#filepick", [CS, MATH])
            try:
                page.wait_for_function(
                    "document.getElementById('s-total') && "
                    "document.getElementById('s-total').textContent !== '0'",
                    timeout=120000,
                )
            except Exception as e:
                ok = False
                print(f"!! courses did not render: {e}")

        if ok:
            # --- MA105 grouping: List view, count rows whose code == 'MA 105' ---
            page.evaluate("state.view='list'; state.distinct=true; refresh();")
            ma105_rows = page.evaluate(
                "[...document.querySelectorAll('#view-list td.code')]"
                ".filter(td=>td.textContent.trim()==='MA 105').length"
            )
            print(f"== MA 105 rows in List view: {ma105_rows} (expect 4 divisions, not ~40)")

            # --- unscheduled bucket (shared #view-extras block, present in every view) ---
            page.evaluate("state.view='grid'; refresh();")
            unsched = page.evaluate(
                "(()=>{const h=document.getElementById('view-extras').innerHTML;"
                "return {hasSection:/Unscheduled \\/ no fixed time/.test(h),"
                " ma593:/MA 593/.test(h), si593:/SI 593/.test(h)}})()"
            )
            print(f"== unscheduled bucket: {unsched}")

            # --- plan mode: add two SAME-slot courses (CS 213 & SI 423, slot 1) -> clash ---
            page.evaluate("['CS 213','SI 423'].forEach(k=>{ if(!plan[k]) togglePlan(k); });")
            plan1 = page.evaluate(
                "({count:Object.keys(plan).length, clash:[...clashKeys],"
                " credits:UNITS.filter(inPlan).reduce((s,u)=>s+creditOf(u),0),"
                " hours:Math.round(UNITS.filter(inPlan).reduce((s,u)=>s+(+u.contact_hours||0),0)*10)/10,"
                " totalsTxt:document.getElementById('planTotals').textContent.replace(/\\s+/g,' ').trim(),"
                " sPlan:document.getElementById('s-plan').textContent})"
            )
            print(f"== plan after add: {plan1}")

            # --- override CS 213 credits, confirm total moves ---
            page.evaluate("setOverride('CS 213', 99);")
            cred_after = page.evaluate("UNITS.filter(inPlan).reduce((s,u)=>s+creditOf(u),0)")
            page.evaluate("setOverride('CS 213','');")  # clear override
            print(f"== plan credits with CS213=99 override: {cred_after}")

            # --- remove one -> clash clears ---
            page.evaluate("togglePlan('SI 423');")
            plan2 = page.evaluate("({count:Object.keys(plan).length, clash:clashKeys.size})")
            print(f"== plan after remove: {plan2}")

            # --- CREDITS: division-invariant, hours-based formula + type overrides ---
            page.evaluate("state.view='grid'; refresh();")   # grid shows credit labels (.ltp)
            creds = page.evaluate(
                "(()=>{const cr=c=>UNITS.filter(u=>u.code===c).map(u=>u.est_credits);"
                "return {ma105:cr('MA 105'), ma419:cr('MA 419'), cs213:cr('CS 213'),"
                " cs293:cr('CS 293'), mas801:cr('MAS801'), ma593:cr('MA 593'), cs490:cr('CS 490'),"
                " ltp_ma105:UNITS.filter(u=>u.code==='MA 105').map(u=>u.ltp),"
                " hours_all_int:UNITS.every(u=>Number.isInteger(u.contact_hours)),"
                " ltp_dom:[...document.querySelectorAll('.ltp')].length}})()"
            )
            print(f"== credits: {creds}")

            # --- EXTRAS/UNSCHEDULED reactivity: planning a unit there highlights it at once ---
            # MA 419 (div '') has an X tutorial -> Tutorials & extras; MAS801 -> Unscheduled
            def extras_planned():
                return page.evaluate(
                    "[...document.querySelectorAll('#view-extras .unit-chip.planned')].length")
            react_before = extras_planned()
            page.evaluate("['MA 419','MAS801'].forEach(k=>{ if(!plan[k]) togglePlan(k); });")
            react_after = extras_planned()
            page.evaluate("['MA 419','MAS801'].forEach(k=>{ if(plan[k]) togglePlan(k); });")
            react_cleared = extras_planned()
            react = {"before": react_before, "after_add": react_after, "after_remove": react_cleared}
            print(f"== extras/unscheduled planned-chips reactivity: {react}")

            # --- filter chips: 'none' empties the dept filter, 'all' restores it ---
            chips = page.evaluate("""(()=>{
              const q=s=>document.querySelectorAll(s).length,
                    st=()=>({on:q("#depts .chip.tgl:not(.off)"),
                             rows:q("#view-list tbody tr, #view-grid .card")});
              state.view='list'; refresh();
              const before=st();
              document.querySelector("#depts .chip.mini.none").click();
              const none=st();
              document.querySelector("#depts .chip.mini.all").click();
              const all=st();
              state.view='grid'; refresh();   // leave the view as later checks expect
              return {before, none, all};
            })()""")
            print(f"== dept chips all/none: {chips}")

            # --- Feature 1: WHY a course is restricted (reasons vs a mismatching profile) ---
            restrict = page.evaluate("""(()=>{
              const saved={...profile}; let found=null;
              outer: for(const d of DEPTS){ for(const yr of ['1','2','3','4','5']){
                profile.dept=d; profile.prog=''; profile.year=yr; profile.batch=batchFromYear(+yr);
                UNITS.forEach(annotate);
                const u=UNITS.find(x=>x._elig==='restricted'||x._elig==='restrictedov');
                if(u){ found={dept:d,year:yr,code:u.code,elig:u._elig,
                              reasons:restrictionReasons(u).map(z=>z.text), text:restrictText(u)}; break outer; }
              }}
              Object.assign(profile,saved); UNITS.forEach(annotate);      // restore profile
              return found;
            })()""")
            print(f"== restriction reasons: {restrict}")

            # --- Feature 2: separate "When I'm busy" TABLE — red cells at day+time,
            #     clash cell for two overlapping planned units, external commitment.
            #     Slot 1 is also marked busy: CS 213 (a shown grid course) sits in
            #     slot 1, so the OLD main-grid tint would have lit its cell — letting
            #     us assert the main grid now carries NO busy tint while the TABLE does. ---
            page.evaluate("['CS 213','SI 423'].forEach(k=>{ if(!plan[k]) togglePlan(k); });"
                          "['1','7'].forEach(s=>state.busy.add(s)); saveBusy(); refresh();")   # clash + external slots 1 & 7
            busy = page.evaluate("""(()=>{
              const rows=busyRows(), days=busyDays();
              const cs=UNITS.find(u=>u.code==='CS 213' && inPlan(u));
              const cm=(cs&&(cs.meetings||[])[0])||null, slot=cm?mslot(cm):null, day=cm?cm.day:null;
              const rowFor=s=>rows.find(r=>Object.values(r.cells).includes(s));
              const rc=slot?rowFor(slot):null, cell=(rc&&day)?busyCellState(rc,day):{state:'?'};
              const r7=rowFor('7'), d7=r7?Object.keys(r7.cells).find(x=>r7.cells[x]==='7'):null;
              return {
                nDays:days.length, nRows:rows.length,
                csSlot:slot, csDay:day, cellState:cell.state, cellCodes:cell.codes||[],
                ext7State: (r7&&d7)?busyCellState(r7,d7).state:'?',
                domRed: document.querySelectorAll('#busyTable td.bt.busy, #busyTable td.bt.clash').length,
                domClash: document.querySelectorAll('#busyTable td.bt.clash').length,
                hasTable: !!document.querySelector('#busyTable table.busytbl'),
                tableInMainGrid: !!document.querySelector('#view-grid .busytbl'),
                gridCells: document.querySelectorAll('#view-grid .cell').length,
                gridBusyCells: document.querySelectorAll('#view-grid .cell.busy').length,
                gridFreeCells: document.querySelectorAll('#view-grid .cell.free').length};
            })()""")
            print(f"== busy table: {busy}")
            page.evaluate("['1','7'].forEach(s=>state.busy.delete(s)); saveBusy();"
                          "['CS 213','SI 423'].forEach(k=>{ if(plan[k]) togglePlan(k); }); refresh();")   # cleanup

            # --- Feature 3: suggest -> confirm core -> pinned, counts, persists ---
            page.evaluate("profile.dept='CS'; profile.prog=''; profile.year='3';"
                          "profile.batch=batchFromYear(3); coreSuggestOpen=true; refresh();")
            core = page.evaluate("""(()=>{
              let cand=suggestCores(), via='suggest';
              if(cand.length){ pinCore(cand[0]); }
              else { via='paste'; const code=(UNITS.find(u=>u.dept==='CS')||UNITS[0]||{}).code;
                     if(code){ document.getElementById('coreInput').value=code; applyCoreText(); } }
              const c=[...cores][0], u=c?firstUnitOf(c):null;
              return {via, candN:cand.length, code:c, pinned:!!(u&&isCorePinned(u)), inPlan:!!(u&&inPlan(u)),
                      planN:UNITS.filter(inPlan).length, badge:!!document.querySelector('.core-badge, .core-chip'),
                      persisted: JSON.parse(localStorage.getItem('asc_cores')||'[]').length};
            })()""")
            print(f"== core suggest/pin: {core}")

            info = {"ma105_rows": ma105_rows, "unsched": unsched, "plan1": plan1,
                    "cred_after": cred_after, "plan2": plan2, "creds": creds, "react": react,
                    "chips": chips, "restrict": restrict, "busy": busy, "core": core}

        # rich state for the screenshot: a clashing plan (MA 105 D1 vs MA 419) +
        # a couple of external commitments, so the busy TABLE shows red + clash cells
        page.evaluate("""(()=>{
          state.view='grid'; coreSuggestOpen=false;
          ['MA 105 · D1','MA 419'].forEach(k=>{ if(!plan[k]) togglePlan(k); });
          ['5','8','9'].forEach(s=>state.busy.add(s)); saveBusy(); refresh();
          const tb=document.querySelector('.toolbar'); if(tb) tb.style.position='static';  // un-stick for a clean shot
        })()""")
        page.locator("#planPanel").screenshot(path=OUT_PNG)   # focus the plan panel (busy table + day headers)
        print(f"== screenshot -> {OUT_PNG}")

        # --- Feature 5: grab.js BUNDLE upload — one .json holding a dept page +
        #     an official crsedetail page; official facts must attach & win ---
        bundle_info = {}
        if ok:
            import json
            detail = ("<html><body><table>"
                      "<tr><td>Fields</td><td>Content</td></tr>"
                      "<tr><td>Course Name</td><td>Data Structures</td></tr>"
                      "<tr><td>Total Credits</td><td>8.0</td></tr>"
                      "<tr><td>Lecture</td><td>3.0</td></tr>"
                      "<tr><td>Tutorial</td><td>1.0</td></tr>"
                      "<tr><td>Practical</td><td>&nbsp;</td></tr>"
                      "<tr><td>Half Semester</td><td>Y</td></tr>"
                      "<tr><td>Description</td><td>Official description via crsedetail.jsp.</td></tr>"
                      "</table></body></html>")
            bundle = {"kind": "asc-bundle", "version": 1, "year": "2026", "semester": "1",
                      "pages": [{"dept": "CS,CSS",
                                 "url": "https://asc.iitb.ac.in/academic/utility/"
                                        "RunningCourses.jsp?deptcd=CS,CSS&year=2026&semester=1",
                                 "html": pathlib.Path(CS).read_text(encoding="utf-8", errors="ignore")}],
                      "details": {"CS 213": {"url": "", "html": detail}}}
            page.evaluate("clearData()")
            page.set_input_files("#filepick", files=[{
                "name": "asc-courses-2026-sem1.json", "mimeType": "application/json",
                "buffer": json.dumps(bundle).encode()}])
            try:
                page.wait_for_function(
                    "document.getElementById('s-total').textContent !== '0'", timeout=120000)
                bundle_info = page.evaluate("""(()=>{
                  const u=UNITS.find(x=>x.code==='CS 213');
                  return u && {official:u.official_credits, src:u.credit_src, est:u.est_credits,
                               credit:creditOf(u), half:u.half_sem, desc:(u.description||'').length,
                               ltp:u.ltp, hsTags:document.querySelectorAll('.tag.hs').length,
                               grabjs:GRAB_JS.length>0,
                               fileLabel:document.querySelector('#fileList .pill')?.textContent||''};
                })()""") or {}
            except Exception as e:
                print(f"!! bundle did not load: {e}")
            print(f"== bundle upload: {bundle_info}")

        browser.close()

        share = check_share_build()
        print(f"== share build (static): {share}")

        print("\n---- console (last 20) ----")
        for line in console_logs[-20:]:
            print(line)
        ext = sorted({b.split('/')[2] for b in blocked if '//' in b})
        print("---- external hosts blocked:", ext or "(none)")

        # Verdict
        c = info.get("creds", {})
        r = info.get("react", {})
        credits_ok = (
            c.get("ma105") == [8, 8, 8, 8]                 # 8 for EVERY division (D1-D4)
            and all(x == 8 for x in c.get("ma419", [])) and c.get("ma419")   # tutorial counted
            and c.get("cs213") == [6]                      # ~3h lecture
            and c.get("cs293") == [3]                      # ~3h lab (P)
            and c.get("mas801") == [4]                     # seminar override
            and all(x == 6 for x in c.get("ma593", [])) and c.get("ma593")   # project -> R&D
            and all(x == 6 for x in c.get("cs490", [])) and c.get("cs490")   # R&D
            and c.get("ltp_ma105") == ["3-1-0", "3-1-0", "3-1-0", "3-1-0"]
            and c.get("hours_all_int") is True             # hr/week integers
            and c.get("ltp_dom", 0) > 0)                   # L-T-P shown in the DOM
        react_ok = (r.get("after_add", 0) > r.get("before", 0)      # extras highlight on add
                    and r.get("after_remove") == r.get("before"))   # and clear on remove
        ch = info.get("chips", {})
        chips_ok = (ch.get("before", {}).get("on", 0) > 0
                    and ch.get("none") == {"on": 0, "rows": 0}
                    and ch.get("all") == ch.get("before"))
        # Feature 1: a restricted course yields a non-empty, profile-aware reason
        rr = info.get("restrict") or {}
        restrict_ok = bool(rr.get("reasons")) and "Restricted" in (rr.get("text") or "")
        # Feature 2: separate busy TABLE renders; two same-slot units -> a CLASH
        # cell; an external slot -> a busy cell; table is NOT inside the main grid
        b = info.get("busy", {})
        busy_ok = (b.get("hasTable") and not b.get("tableInMainGrid")
                   and b.get("cellState") == "clash" and len(b.get("cellCodes", [])) >= 2
                   and b.get("ext7State") == "busy"
                   and b.get("domClash", 0) >= 1 and b.get("domRed", 0) >= 1
                   # main grid renders cells but carries NO busy/free tint class,
                   # even with busy slots 1 & 7 active -> the busy TABLE is the sole
                   # busy indicator (regression guard for the removed .cell.busy tint)
                   and b.get("gridCells", 0) > 0
                   and b.get("gridBusyCells", -1) == 0 and b.get("gridFreeCells", -1) == 0)
        # Feature 3: a core got confirmed -> pinned in plan + persisted + badge shown
        co = info.get("core", {})
        core_ok = (bool(co.get("code")) and co.get("pinned") and co.get("inPlan")
                   and co.get("badge") and co.get("persisted", 0) > 0)
        # Feature 4: share build references CDN + flips FORCE_CDN; offline stays local
        share_ok = all(share.values())
        # Feature 5: the bundle attaches official facts; official credits (8) beat
        # the estimate (6); half-sem tag in the DOM; grab.js embedded; file row "ALL"
        bi = bundle_info
        bundle_ok = (bi.get("official") == 8 and bi.get("src") == "official"
                     and bi.get("est") == 6 and bi.get("credit") == 8
                     and bi.get("half") is True and bi.get("desc", 0) > 0
                     and bi.get("ltp") == "3-1-0" and bi.get("hsTags", 0) > 0
                     and bi.get("grabjs") is True and bi.get("fileLabel") == "ALL")
        good = (ok
                and info.get("ma105_rows") == 4
                and info.get("unsched", {}).get("hasSection")
                and info.get("plan1", {}).get("count") == 2
                and len(info.get("plan1", {}).get("clash", [])) == 2
                and info.get("plan1", {}).get("credits", 0) > 0
                and info.get("plan1", {}).get("hours", 0) > 0
                and info.get("cred_after", 0) >= 99
                and info.get("plan2", {}).get("clash") == 0
                and credits_ok and react_ok and chips_ok
                and restrict_ok and busy_ok and core_ok and share_ok and bundle_ok)
        print(f"\n   credits_ok={credits_ok}  react_ok={react_ok}  chips_ok={chips_ok}"
              f"  restrict_ok={restrict_ok}  busy_ok={busy_ok}  core_ok={core_ok}"
              f"  share_ok={share_ok}  bundle_ok={bundle_ok}")
        print("==== RESULT:", "PASS" if good else "FAIL", "====")
        return 0 if good else 1


sys.exit(run())
