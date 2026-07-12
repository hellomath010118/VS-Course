# Vibe Selecting Courses (VS Course)

> Plan your IIT-Bombay semester by vibes, not by refreshing ASC for the fortieth time.

## What it is

**VS Course** is an offline, browser-based course browser and planner for the
IIT-Bombay **ASC** (Academic Support Console) — the portal you love to hate every
registration season. You feed it your own saved ASC course pages and it turns that
2003-era frameset HTML into something you can actually *plan* with:

- **Filter** courses by time-slot, department, level, and eligibility.
- **Plan electives** with automatic credit estimation (derived from each course's
  L–T–P lecture / tutorial / practical hours).
- **Clash detection** so you stop accidentally enrolling in two courses that meet at
  the same time.
- A weekly **"When I'm busy"** table so your timetable is a grid, not a guess.

Everything runs **locally in your browser** via [Pyodide](https://pyodide.org)
(Python, compiled to WebAssembly). **Your course data never leaves your machine** —
no server, no upload, no account, no telemetry. The page starts empty; you decide
what goes in.

## Quick start (offline, recommended)

```
git clone https://github.com/hellomath010118/VS-Course.git
cd VS-Course
python3 serve.py
```

This opens the tool in your browser and runs **fully offline** using the vendored
`pyodide/` runtime — zero external network requests. You only need **Python 3.8+**
(standard library only; nothing to `pip install`).

> Why a tiny server instead of double-clicking the file? Browsers refuse to load a
> local Python runtime straight off a `file://` page, so `serve.py` hands it over
> `http://localhost` instead. It serves its own folder and opens the tab for you.

## Or just open one file (needs internet)

Prefer not to clone anything? Open **`timetable-share.html`** directly. It's a single,
self-contained page that loads Pyodide from a CDN at runtime — great for quick
sharing, sending to a friend, or opening on your phone. The catch: it **needs an
internet connection** (the offline build above does not).

## Loading your courses

The tool starts **empty** — it ships with no course data, because your course list
is yours.

1. Open the ASC course page you want in your browser.
2. Save it with the **[SingleFile](https://github.com/gildas-lormeau/SingleFile)**
   browser extension (it flattens the whole page into one `.html` file).
3. **Drag-and-drop** that saved `.html` onto the VS Course tool.

Repeat for as many departments as you like — add several, then toggle them on and off
as you compare options. All parsing happens client-side, live.

## Rebuilding (for tinkerers)

`asc_parser.py` is the single source of truth: it's the parser **and** the site
builder, and at build time it embeds its own source into the page so the browser runs
the exact same parser you'd run on the command line.

```
python3 asc_parser.py --build   # offline build  -> timetable.html   (uses ./pyodide/)
python3 asc_parser.py --cdn      # share build    -> timetable-share.html (uses a CDN)
```

Edit `template.html` for layout/styling and `asc_parser.py` for parsing logic, then
rebuild. That's the whole loop.

## A loving roast of every file

Because a repo you can't laugh at is a repo you'll never open again:

- **`asc_parser.py`** — The overachiever. Fluent in BeautifulSoup and the lost
  dialect of ASC's frameset-era HTML, it parses your courses, estimates your credits,
  builds the website, *and* smuggles a copy of itself into every page it generates.
  Single source of truth, absolutely zero chill.
- **`template.html`** — 1,400 lines of HTML doing its best impression of a real web
  app. It's the skeleton, the styling, and the JavaScript glue — frozen in the split
  second before `asc_parser.py` pours an entire Python interpreter into it.
- **`timetable.html`** — The offline build. A 120 KB page that quietly boots a 14 MB
  Python runtime the instant you open it, then runs with your Wi-Fi switched off out
  of sheer principle.
- **`timetable-share.html`** — Its extrovert twin. One file, no folder, borrows Python
  from a CDN. Perfect for emailing to a friend who will open it on campus Wi-Fi and
  then blame *you* when it takes a moment to wake up.
- **`serve.py`** — Exists purely because your browser refuses to trust files living on
  your own hard drive. A tiny localhost server whose entire life's work is opening one
  browser tab. It has made peace with this.
- **`pyodide/`** — 14 MB of "it was genuinely easier to ship an entire Python
  interpreter to the browser than to trust JavaScript with dates and time-slots." Do
  not open it. Do not question it. It Just Works, and that is a gift.
- **`README.md`** — You're soaking in it. It roasts every other file in the repo and
  then, in a stunning display of courage, roasts itself. Roughly 40% instructions,
  60% coping mechanism. Ten out of ten, would document again.

---

Made for surviving ASC registration. Data stays on your machine. Course clashes are
your responsibility; blaming the portal is your right.
