/* Web Worker: runs the viewer's Python solution under our tracer (public/py/tracer.py)
   inside Pyodide and posts back the raw event list. Pyodide (~7 MB) is fetched from the
   jsDelivr CDN on the first run and cached by the browser afterwards. */
const PYODIDE = "https://cdn.jsdelivr.net/pyodide/v0.27.7/full/";
let ready = null;

async function init(base) {
  importScripts(PYODIDE + "pyodide.js");
  const py = await self.loadPyodide({ indexURL: PYODIDE });
  const src = await (await fetch(base + "/py/tracer.py")).text();
  py.FS.writeFile("/home/pyodide/tracer.py", src);
  py.runPython("import sys\nsys.path.insert(0, '/home/pyodide')\nimport tracer");
  return py;
}

const RUN = `
import json, traceback
import tracer as T
def _ev(events):
    return [dict(kind=e.kind, line=e.line, **e.data) for e in events]
t = T.Trace()
nums = T.TracedList(list(NUMS), t, "nums")
try:
    result, trace = T.run_traced(USER_CODE, "rob", (nums,), helpers={"memo": T.TracedDict(t)}, trace=t, max_events=3000)
    out = {"result": result, "events": _ev(trace.events)}
except Exception as ex:
    tb = traceback.format_exc().strip().splitlines()
    out = {"error": f"{type(ex).__name__}: {ex}", "trace": tb[-3:], "events": _ev(t.events)}
json.dumps(out, default=str)
`;

self.onmessage = async (e) => {
  const { id, base, code, nums } = e.data;
  try {
    if (!ready) { self.postMessage({ id, status: "loading" }); ready = init(base); }
    const py = await ready;
    self.postMessage({ id, status: "running" });
    py.globals.set("USER_CODE", code);
    py.globals.set("NUMS", py.toPy(nums));
    const out = JSON.parse(py.runPython(RUN));
    self.postMessage({ id, status: "done", ...out });
  } catch (err) {
    ready = null;
    self.postMessage({ id, status: "error", error: String(err) });
  }
};
