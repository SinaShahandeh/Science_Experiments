"""Run the agent's analysis code: a separate Python process with no network and writes only to the run folder.

On macOS this uses sandbox-exec with a profile that denies all network access (so the code can't reach
the hardware service) and all file writes outside the run folder and temporary directories.

On Linux it uses bubblewrap (`bwrap`): the code sees only /usr, the harness's Python environment, the
recorded sessions (read-only) and the run folder, in its own namespaces with no network and no view of
other processes. Nothing else on the host is visible, so a public deployment's API key (in the harness's
environment and a root-only file) is out of reach. prlimit caps memory, CPU time and file size.

The interpreter is the harness's own (numpy, scipy, pandas, matplotlib available). Set REQUIRE = True
(the harness does with --public) to refuse to run code when no sandbox is available.
"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .env import ROOT

PROFILE = """(version 1)
(allow default)
(deny network*)
(deny file-write*)
(allow file-write* (subpath "{run}") (subpath "/private/var/folders") (subpath "/private/tmp")
                   (subpath "/dev") (literal "/dev/null"))
"""
MAX_OUT = 6000
READ_ONLY = [ROOT / "sim_sessions", ROOT / "sessions"]   # recorded well images, for analysis
REQUIRE = False


def _macos(run_dir, cmd):
    if not shutil.which("sandbox-exec"):
        return None
    return ["sandbox-exec", "-p", PROFILE.format(run=str(run_dir))] + cmd


def _linux(run_dir, cmd, timeout):
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return None
    args = [bwrap, "--unshare-all", "--die-with-parent", "--new-session", "--hostname", "sandbox",
            "--ro-bind", "/usr", "/usr", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    for link in ("bin", "sbin", "lib", "lib64", "lib32"):   # merged /usr: these are symlinks into it
        p = Path("/") / link
        if p.is_symlink():
            args += ["--symlink", os.readlink(p), str(p)]
        elif p.is_dir():
            args += ["--ro-bind", str(p), str(p)]
    for f in ("/etc/localtime", "/etc/ld.so.cache"):
        args += ["--ro-bind-try", f, f]
    for p in {Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()}:
        if not str(p).startswith("/usr"):
            args += ["--ro-bind", str(p), str(p)]
    for p in READ_ONLY:
        if p.is_dir():
            args += ["--ro-bind", str(p.resolve()), str(p.resolve())]
    args += ["--bind", str(run_dir), str(run_dir), "--chdir", str(run_dir), "--"] + cmd
    prlimit = shutil.which("prlimit")
    if prlimit:
        args = [prlimit, f"--as={3 << 30}", f"--fsize={200 << 20}", f"--cpu={int(timeout) + 10}", "--"] + args
    return args


def run_python(code, run_dir, step, timeout=120):
    """Write code to analysis/step_NNN.py, run it in run_dir, return (ok, text, new_figures)."""
    run_dir = Path(run_dir).resolve()
    (run_dir / "analysis").mkdir(exist_ok=True)
    (run_dir / "figures").mkdir(exist_ok=True)
    (run_dir / ".mpl").mkdir(exist_ok=True)
    script = run_dir / "analysis" / f"step_{step:03d}.py"
    script.write_text(code)
    before = {p: p.stat().st_mtime for p in (run_dir / "figures").glob("*")}
    env = {"PATH": "/usr/bin:/bin", "MPLCONFIGDIR": str(run_dir / ".mpl"), "MPLBACKEND": "Agg",
           "HOME": os.environ.get("HOME", str(run_dir)), "PYTHONDONTWRITEBYTECODE": "1", "LANG": "en_US.UTF-8"}
    cmd = [sys.executable, "-E", "-B", str(script)]  # keep the user site-packages (matplotlib needs packaging from there)
    if sys.platform == "darwin":
        wrapped = _macos(run_dir, cmd)
    else:
        env.update(HOME="/tmp", LANG="C.UTF-8", OPENBLAS_NUM_THREADS="2", OMP_NUM_THREADS="2")
        wrapped = _linux(run_dir, cmd, timeout)
    sandboxed = wrapped is not None
    if not sandboxed and REQUIRE:
        return False, "[not run: no sandbox is available on this host, and this deployment requires one]", []
    t0 = time.time()
    try:
        p = subprocess.run(wrapped or cmd, cwd=run_dir, env=env, capture_output=True, text=True, timeout=timeout)
        ok, out, err = p.returncode == 0, p.stdout, p.stderr
        status = f"exit {p.returncode}"
    except subprocess.TimeoutExpired as e:
        ok, out, err = False, (e.stdout or "") if isinstance(e.stdout, str) else "", f"timed out after {timeout} s"
        status = "timeout"
    figs = sorted(p for p in (run_dir / "figures").glob("*.png") if before.get(p) != p.stat().st_mtime)
    text = f"[{status}, {time.time() - t0:.1f} s{'' if sandboxed else ', NOT sandboxed'}]\n"
    if out:
        text += "stdout:\n" + (out if len(out) <= MAX_OUT else out[:MAX_OUT // 2] + "\n…\n" + out[-MAX_OUT // 2:])
    if err:
        err = err if len(err) <= MAX_OUT else "…" + err[-MAX_OUT:]
        text += ("\n" if out else "") + "stderr:\n" + err
    if figs:
        text += "\nfigures: " + ", ".join(f"figures/{f.name}" for f in figs)
    return ok, text, figs
