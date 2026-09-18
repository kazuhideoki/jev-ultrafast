"""Real-browser decorated dropdown checks. No model calls or external websites."""

import json
from contextlib import closing
from unittest.mock import patch
from urllib.parse import quote

from jev_ultrafast.browser import Browser, StalePage

HTML = """<!doctype html><style>
body{margin:30px}.wrap{display:inline-block;position:relative}select{width:180px;height:30px}
select.decorated{position:absolute;opacity:.01;z-index:-1}
#surface{display:inline-block;width:180px;height:30px;cursor:pointer}
</style><div class="wrap"><select class="decorated" aria-label="Category" id="native">
<option>All</option><option>Design</option></select>
<span tabindex="-1" aria-hidden="true" id="surface"
 onclick="window.opens++;document.querySelector('#menu').hidden=false">
Category All</span></div><div id="menu" hidden>
<button role="option" onclick="window.picks++;native.selectedIndex=1;menu.hidden=true">Design</button></div>
<script>window.opens=0;window.picks=0;</script>"""


def main():
    results = []
    with closing(Browser("data:text/html," + quote(HTML))) as b:
        p = b.observe(screenshot=False)
        assert not any(a["kind"] == "select" for a in p["actions"])
        a = next(a for a in p["actions"] if a.get("proxy_for"))
        assert a["kind"] == "click" and a["label"] == "Category All"
        b.act(a, p)
        results.append("surface clicked once")
        p = b.observe(screenshot=False)
        a = next(a for a in p["actions"] if a["label"] == "Design" and a["kind"] == "click")
        b.act(a, p)
        results.append("observed menu option clicked once")
        assert b.evaluate("[native.value,opens,picks]") == ["Design", 1, 1]
    for label, mutation in {
        "global overlay": "const cover=document.createElement('div');"
                          "cover.style.cssText='position:fixed;inset:0;z-index:9999';document.body.append(cover)",
        "unrelated sibling overlay": "surface.textContent='Different control'",
        "disabled backing control": "native.disabled=true",
        "no interaction marker": "surface.removeAttribute('tabindex')",
    }.items():
        with closing(Browser("data:text/html," + quote(HTML))) as b:
            b.evaluate(mutation)
            p = b.observe(screenshot=False)
            assert not any(a.get("proxy_for") or a["kind"] == "select" for a in p["actions"]), label
            assert b.evaluate("[native.value,opens,picks]") == ["All", 0, 0]
            results.append(label + ": not offered or mutated")
    with closing(Browser("data:text/html," + quote(HTML))) as b:
        b.evaluate("surface.remove();native.className=''")
        p = b.observe(screenshot=False)
        a = next(a for a in p["actions"] if a["kind"] == "select")
        b.act(a, p)
        assert b.evaluate("native.value") == "Design"
        results.append("ordinary native dropdown still works")
    with closing(Browser("data:text/html," + quote(HTML))) as b:
        p = b.observe(screenshot=False)
        a = next(a for a in p["actions"] if a.get("proxy_for"))
        b.evaluate("surface.textContent='Different control'")
        with patch.object(b, "fresh", return_value=True):
            try:
                b.act(a, p)
            except StalePage:
                pass
            else:
                raise AssertionError("changed surface executed")
        assert b.evaluate("[opens,picks]") == [0, 0]
        results.append("executor revalidates surface relation after observation")

    # Menus may appear asynchronously after the click has already been logged.
    with closing(Browser("data:text/html," + quote(HTML))) as b:
        b.evaluate("surface.onclick=()=>{opens++;setTimeout(()=>menu.hidden=false,250)}")
        p = b.observe(screenshot=False)
        a = next(a for a in p["actions"] if a.get("proxy_for"))
        b.act(a, p)
        p = b.observe(screenshot=False)
        assert b.evaluate("opens") == 1
        assert any(a["kind"] == "click" and a["label"] == "Design" for a in p["actions"])
        results.append("asynchronous menu awaited without a second click")
    print(json.dumps({"passed": len(results), "checks": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
