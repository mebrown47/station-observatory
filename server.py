#!/usr/bin/env python3
# Copyright (c) 2026 Mike Brown
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Station Observatory: one page for the health of every receiver on a feeder station

The link dashboards (iridium-observatory, vdl2-observatory, inmarsat-rx's Observatory) each show
what one receiver hears. This page sits above them and shows whether the station is doing its job:
is each receiver running and on which SDR, are its feeds open, is its database keeping up, has it
logged trouble, and how is the host. It only reads (systemd, /proc, /sys, the journal, logs and the
link databases), so it can run beside them without changing anything. Stdlib only.

  GET /            -> the page
  GET /api/status  -> its data
  GET /healthz     -> ok

Usage:
    server.py [--config station.json] [--port 8895] [--host 127.0.0.1]

What to watch comes from the config (station.example.json shows the layout): per link, its
receiver, dashboard and rollup (a systemd unit, or a process matched by its command line), its SDR
(by serial), database queries for the newest record and messages per hour, a log to scan for
trouble, and its pages. It binds to 127.0.0.1 by default: the page shows command lines, feed
destinations and paths, so reach it through an SSH tunnel.
"""

import argparse
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8895
HZ = os.sysconf("SC_CLK_TCK")
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
# USB SDRs by vendor:product.
SDR_IDS = {
    "1d50:60a1": "Airspy R2/Mini", "03eb:800c": "Airspy HF+", "1d50:6089": "HackRF One", "1d50:cc15": "RTL-SDR Blog",
    "0bda:2838": "RTL-SDR", "0bda:2832": "RTL-SDR", "1df7:3000": "SDRplay RSP1A", "1df7:3010": "SDRplay RSP2",
    "1df7:3020": "SDRplay RSPduo", "1df7:3050": "SDRplay RSPdx", "2500:0020": "USRP B2xx", "2cf0:5250": "bladeRF 2.0",
}
# Thresholds, overridable per link in the config.
STALE_WARN_S = 900       # Newest record older than this: amber.
STALE_BAD_S = 3600       # ...and this: red.
ROLLUP_WARN_S = 900      # A rollup timer that has not run for this long: amber.
ALERTS = r"(?i)\b(error|fail(ed|ure)?|dropped|overrun|timeout|lost)\b"


def run(cmd, timeout=5):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


# ---- Host ----

def host_info(paths):
    up = float(open("/proc/uptime").read().split()[0])
    load = [float(x) for x in open("/proc/loadavg").read().split()[:3]]
    mem = {}
    for line in open("/proc/meminfo"):
        k, v = line.split(":", 1)
        mem[k] = int(v.split()[0]) * 1024
    disks, seen = [], set()
    for p in ["/"] + paths:
        try:
            st = os.stat(p)
        except OSError:
            continue
        if st.st_dev in seen:
            continue
        seen.add(st.st_dev)
        d = shutil.disk_usage(p)
        disks.append({"path": p, "total": d.total, "used": d.used, "free": d.free})
    temps = []
    for z in sorted(os.listdir("/sys/class/thermal")) if os.path.isdir("/sys/class/thermal") else []:
        try:
            kind = open(f"/sys/class/thermal/{z}/type").read().strip()
            temps.append({"kind": kind, "c": int(open(f"/sys/class/thermal/{z}/temp").read()) / 1000})
        except (OSError, ValueError):
            pass
    ntp = run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"]).strip()
    return {"hostname": socket.gethostname(), "uptime": up, "load": load, "cpus": os.cpu_count(),
            "mem_total": mem.get("MemTotal", 0), "mem_avail": mem.get("MemAvailable", 0),
            "disks": disks, "temp": max((t["c"] for t in temps), default=None),
            "ntp": ntp == "yes" if ntp else None, "kernel": os.uname().release}


# ---- Processes ----

_prev_cpu = {}


def processes():
    """pid -> {cmd, argv0, start, cpu, rss, usb: [paths]} for every process this user can see."""
    boot = time.time() - float(open("/proc/uptime").read().split()[0])
    now = time.monotonic()
    out = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        pid = int(d)
        try:
            raw = open(f"/proc/{pid}/cmdline", "rb").read()
            if not raw:
                continue
            argv = raw.rstrip(b"\0").split(b"\0")
            stat = open(f"/proc/{pid}/stat").read()
            f = stat[stat.rindex(")") + 2:].split()
            ticks = int(f[11]) + int(f[12])
            start = boot + int(f[19]) / HZ
            rss = int(f[21]) * PAGE_SIZE
        except (OSError, ValueError, IndexError):
            continue
        usb = []
        try:
            for fd in os.listdir(f"/proc/{pid}/fd"):
                try:
                    t = os.readlink(f"/proc/{pid}/fd/{fd}")
                except OSError:
                    continue
                if t.startswith("/dev/bus/usb/"):
                    usb.append(t)
        except OSError:
            pass   # Another user's process: no fds, but still listed.
        prev = _prev_cpu.get(pid)
        if prev and prev[2] == start and now > prev[1]:
            cpu = (ticks - prev[0]) / HZ / (now - prev[1]) * 100
        else:
            cpu = ticks / HZ / max(1, time.time() - start) * 100   # Average since it started.
        _prev_cpu[pid] = (ticks, now, start)
        out[pid] = {"pid": pid, "cmd": " ".join(a.decode(errors="replace") for a in argv),
                    "argv0": os.path.basename(argv[0].decode(errors="replace")), "start": start, "cpu": round(cpu, 1),
                    "rss": rss, "usb": usb}
    for pid in list(_prev_cpu):
        if pid not in out:
            del _prev_cpu[pid]
    return out


def sockets():
    """pid -> [{proto, state, peer}] for sockets with a remote peer (feeds), from ss."""
    out = {}
    for line in run(["ss", "-tunapH"]).splitlines():
        p = line.split()
        if len(p) < 6:
            continue
        m = re.search(r"pid=(\d+)", line)
        if not m:
            continue
        peer = p[5]
        if peer.startswith(("*", "0.0.0.0", "[::]")) or p[1] == "LISTEN":
            continue
        out.setdefault(int(m.group(1)), []).append({"proto": p[0], "state": p[1], "peer": peer, "local": p[4]})
    return out


# ---- systemd ----

UNIT_PROPS = ["Id", "LoadState", "ActiveState", "SubState", "MainPID", "NRestarts", "ActiveEnterTimestamp",
              "Result", "LastTriggerUSec", "Description", "ExecStart"]


def units(names):
    out = {}
    if not names:
        return out
    block = {}
    for line in run(["systemctl", "show", *names, "-p", ",".join(UNIT_PROPS)]).splitlines() + [""]:
        if not line.strip():
            if block.get("Id"):
                out[block["Id"]] = block
            block = {}
            continue
        k, _, v = line.partition("=")
        block[k] = v
    return out


def systemd_time(s):
    """'Tue 2026-10-27 09:09:00 CDT' -> epoch, or None."""
    if not s or s == "n/a":
        return None
    out = run(["date", "-d", s, "+%s"]).strip()
    return int(out) if out.isdigit() else None


# ---- Logs ----

_file_counts = {}   # path -> {"offset", "total", "history": [(t, total)], "last": line}


def file_alerts(path, pattern):
    """Matches of pattern in a growing log, read incrementally; counts per hour come from this
    server's own history of the running total (log lines here carry no timestamps)."""
    st = _file_counts.setdefault(path, {"offset": 0, "total": 0, "history": [], "last": None, "since": time.time()})
    try:
        size = os.path.getsize(path)
        if size < st["offset"]:   # Rotated or truncated.
            st.update(offset=0, total=0, history=[], since=time.time())
        with open(path, "rb") as f:
            f.seek(st["offset"])
            data = f.read(64 << 20)
            st["offset"] += len(data)
        rx = re.compile(pattern)
        for line in data.decode(errors="replace").splitlines():
            if rx.search(line):
                st["total"] += 1
                st["last"] = line.strip()[:200]
    except OSError as e:
        return {"error": str(e)}
    now = time.time()
    st["history"].append((now, st["total"]))
    st["history"] = [h for h in st["history"] if now - h[0] <= 86400 + 600]
    at = lambda age: next((tot for t, tot in st["history"] if now - t <= age), st["total"])
    return {"hour": st["total"] - at(3600), "day": st["total"] - at(86400), "total": st["total"], "last": st["last"],
            "since": st["since"], "source": "file"}


def journal_alerts(unit, pattern):
    def grep(since):
        return [l for l in run(["journalctl", "-u", unit, "--since", since, "-g", pattern, "-q", "--no-pager",
                                "-o", "short-unix"], timeout=10).splitlines() if l.strip()]
    day = grep("-24h")
    hour = [l for l in day if _unix(l) and time.time() - _unix(l) <= 3600]
    last = day[-1] if day else None
    return {"hour": len(hour), "day": len(day), "last": last.split(" ", 2)[-1][:200] if last else None,
            "last_t": _unix(last) if last else None, "source": "journal"}


def _unix(line):
    try:
        return float(line.split(" ", 1)[0])
    except (ValueError, AttributeError):
        return None


# ---- Databases ----

def db_info(spec):
    path = spec.get("path")
    if not path or not os.path.exists(path):
        return {"error": "no database at " + str(path)}
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        try:
            latest = con.execute(spec["latest"]).fetchone()[0] if spec.get("latest") else None
            hourly = []
            if spec.get("hourly"):
                h0 = int(time.time() // 3600 * 3600) - 23 * 3600
                got = dict(con.execute(spec["hourly"], (h0,)).fetchall())
                hourly = [{"hour": h, "n": got.get(h, 0)} for h in range(h0, h0 + 24 * 3600, 3600)]
        finally:
            con.close()
        return {"latest": latest, "hourly": hourly, "size": os.path.getsize(path)}
    except sqlite3.Error as e:
        return {"error": str(e)}


# ---- Putting it together ----

def find_proc(procs, match, own):
    """The process whose command line contains `match`, not a shell or ssh wrapping it."""
    best = None
    for p in procs.values():
        if p["pid"] in own or match not in p["cmd"] or p["argv0"] in ("bash", "sh", "ssh", "sudo", "timeout", "nohup"):
            continue
        if best is None or p["start"] < best["start"]:
            best = p
    return best


def component(spec, units_, procs, socks, own):
    """A receiver, dashboard or rollup: state, process figures, sockets."""
    if not spec:
        return None
    c = {"label": spec.get("label") or spec.get("unit") or spec.get("match") or spec.get("fresh_file")}
    p = None
    if spec.get("fresh_file"):
        # A job with no unit, such as a rollup loop in a script: judged by when it last wrote its log.
        try:
            c["last_run"] = os.path.getmtime(spec["fresh_file"])
        except OSError:
            c["last_run"] = None
        c["active"] = "active" if c["last_run"] else "inactive"
        c["ok"] = c["last_run"] is not None and time.time() - c["last_run"] < spec.get("warn_s", ROLLUP_WARN_S)
        return c
    if spec.get("unit"):
        u = units_.get(spec["unit"], {})
        c.update(unit=spec["unit"], load=u.get("LoadState"), active=u.get("ActiveState"), sub=u.get("SubState"),
                 restarts=int(u.get("NRestarts") or 0), result=u.get("Result"))
        if spec["unit"].endswith(".timer"):
            last = u.get("LastTriggerUSec")
            c["last_run"] = systemd_time(last)
            c["ok"] = c["active"] == "active" and c["last_run"] is not None and \
                time.time() - c["last_run"] < spec.get("warn_s", ROLLUP_WARN_S)
            return c
        pid = int(u.get("MainPID") or 0)
        p = procs.get(pid)
        c["ok"] = c["active"] == "active"
        m = re.search(r"argv\[\]=([^;]*)", u.get("ExecStart") or "")
        if m:
            c["configured_cmd"] = m.group(1).strip()   # What it runs when started: its feeds even while down.
    else:
        p = find_proc(procs, spec["match"], own)
        c.update(active="active" if p else "inactive", sub="running" if p else "not running", ok=bool(p))
    if p:
        c.update(pid=p["pid"], cmd=p["cmd"], cpu=p["cpu"], rss=p["rss"], started=p["start"], usb=p["usb"],
                 sockets=socks.get(p["pid"], []))
    return c


def feeds_from(cmd):
    """Feed destinations named on a receiver's command line: --udp/-u HOST:PORT (several, or
    comma-separated), --feed=udp://HOST:PORT, --basestation=HOST:PORT."""
    out = []
    toks = cmd.split()
    for i, t in enumerate(toks):
        val = None
        m = re.match(r"--(udp|feed|basestation)=(.+)", t)
        if m:
            kind, val = m.group(1), m.group(2)
        elif t in ("--udp", "-u") and i + 1 < len(toks):
            kind, val = "udp", toks[i + 1]
        if not val:
            continue
        for v in val.split(","):
            v = re.sub(r"^\w+://", "", v)
            if ":" in v and not v.isdigit():
                out.append({"kind": "basestation" if kind == "basestation" else "udp", "dest": v})
    return out


def resolve_peer(dest):
    host, _, port = dest.rpartition(":")
    try:
        return {f"{a[4][0]}:{port}" for a in socket.getaddrinfo(host.strip("[]"), port)}
    except OSError:
        return set()


_resolved = {}


def link_status(link, units_, procs, socks, sdrs, own):
    rx = component(link.get("receiver"), units_, procs, socks, own)
    dash = component(link.get("dashboard"), units_, procs, socks, own)
    roll = component(link.get("rollup"), units_, procs, socks, own)
    db = db_info(link.get("db", {}))
    log = link.get("log") or {}
    pat = log.get("pattern", ALERTS)
    alerts = journal_alerts(log["journal"], pat) if log.get("journal") else file_alerts(log["file"], pat) if log.get("file") else None
    problems, warnings = [], []

    # Feeds: each destination on the command line, and whether the receiver holds a socket to it.
    feeds = []
    if rx and not rx.get("cmd") and rx.get("configured_cmd"):
        for f in feeds_from(rx["configured_cmd"]):
            feeds.append({**f, "state": None, "proto": "tcp" if f["kind"] == "basestation" else "udp", "ok": False, "down": True})
    if rx and rx.get("cmd"):
        for f in feeds_from(rx["cmd"]):
            if f["dest"] not in _resolved or time.time() - _resolved[f["dest"]][0] > 600:
                _resolved[f["dest"]] = (time.time(), resolve_peer(f["dest"]))
            peers = _resolved[f["dest"]][1]
            s = next((s for s in rx.get("sockets", []) if s["peer"] in peers or s["peer"].replace("[", "").replace("]", "") in peers), None)
            f["state"] = s["state"] if s else None
            f["proto"] = s["proto"] if s else ("tcp" if f["kind"] == "basestation" else "udp")
            # UDP gives no delivery confirmation: an open socket is all there is to see.
            f["ok"] = bool(s) and (s["proto"] == "udp" or s["state"] == "ESTAB")
            if not f["ok"]:
                warnings.append(f"feed to {f['dest']}: " + ("no socket" if not s else s["state"].lower()))
            feeds.append(f)

    # SDR: the one the config names, and who holds it.
    sdr = None
    if link.get("sdr"):
        sdr = next((d for d in sdrs if link["sdr"].upper() in (d["serial"] or "").upper()), None)
        if not sdr:
            problems.append("its SDR (" + link["sdr"] + ") is not plugged in")
    held = sdr and rx and not rx.get("ok") and sdr["holders"]

    def missing(c):
        # A unit systemd has never heard of: not installed, which says more than "not running".
        return c.get("load") == "not-found"

    if rx and missing(rx):
        problems.append(f"receiver not installed (no {rx['unit']})")
    elif not rx or not rx.get("ok"):
        # When another program has its SDR, that is why: say so in the same line.
        problems.append("receiver not running" + (": its SDR is held by " + ", ".join(h["name"] for h in sdr["holders"]) if held else ""))
    elif rx.get("restarts"):
        warnings.append(f"receiver restarted {rx['restarts']}× since the unit started")
    if dash and missing(dash):
        warnings.append(f"dashboard not installed (no {dash['unit']})")
    elif dash and not dash.get("ok"):
        warnings.append("dashboard not running")
    if roll and missing(roll):
        warnings.append(f"rollup not installed (no {roll['unit']})")
    elif roll and not roll.get("ok"):
        warnings.append("rollup has not run recently" if roll.get("active") == "active" else "rollup timer not active")
    age = time.time() - db["latest"] if db.get("latest") else None
    db["age"] = age
    if db.get("error"):
        warnings.append("database: " + db["error"])
    elif age is not None and rx and rx.get("ok"):
        if age > link.get("stale_bad_s", STALE_BAD_S):
            problems.append(f"no new records for {fmt_age(age)}")
        elif age > link.get("stale_warn_s", STALE_WARN_S):
            warnings.append(f"no new records for {fmt_age(age)}")
    if alerts and alerts.get("hour"):
        warnings.append(f"{alerts['hour']} log alert{'s' if alerts['hour'] > 1 else ''} in the last hour")
    state = "bad" if problems else "warn" if warnings else "ok"
    return {"name": link["name"], "what": link.get("what", ""), "state": state, "problems": problems, "warnings": warnings,
            "receiver": rx, "dashboard": dash, "rollup": roll, "db": db, "alerts": alerts, "feeds": feeds,
            "sdr": sdr["serial"] if sdr else link.get("sdr"), "pages": link.get("pages", [])}


def fmt_age(s):
    s = int(s)
    return f"{s // 86400} d {s % 86400 // 3600} h" if s >= 86400 else f"{s // 3600} h {s % 3600 // 60} min" if s >= 3600 else \
        f"{s // 60} min" if s >= 60 else f"{s} s"


def usb_sdrs(procs, names):
    out = []
    base = "/sys/bus/usb/devices"
    for d in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        p = os.path.join(base, d)
        try:
            vid = open(p + "/idVendor").read().strip()
            pid = open(p + "/idProduct").read().strip()
        except OSError:
            continue
        kind = SDR_IDS.get(f"{vid}:{pid}")
        if not kind:
            continue
        rd = lambda f: open(os.path.join(p, f)).read().strip() if os.path.exists(os.path.join(p, f)) else ""
        node = "/dev/bus/usb/%03d/%03d" % (int(rd("busnum") or 0), int(rd("devnum") or 0))
        holders = [{"pid": q["pid"], "name": names.get(q["pid"], q["argv0"])} for q in procs.values() if node in q["usb"]]
        out.append({"kind": kind, "id": f"{vid}:{pid}", "serial": rd("serial"), "product": rd("product"), "port": d,
                    "node": node, "speed": rd("speed"), "holders": holders})
    return out


class Station:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.cache, self.at = None, 0

    def status(self):
        with self.lock:
            if self.cache and time.time() - self.at < self.cfg.get("refresh_s", 15):
                return self.cache
            self.cache, self.at = self.collect(), time.time()
            return self.cache

    def collect(self):
        cfg = self.cfg
        procs = processes()
        own = {os.getpid()}
        socks = sockets()
        names_ = [c["unit"] for l in cfg["links"] for c in (l.get("receiver"), l.get("dashboard"), l.get("rollup")) if c and c.get("unit")]
        units_ = units(names_)
        # Name each receiver's process, so the SDR table can say who holds what.
        pidname = {}
        for l in cfg["links"]:
            for role in ("receiver", "dashboard"):
                c = l.get(role)
                if not c:
                    continue
                if c.get("unit"):
                    pid = int(units_.get(c["unit"], {}).get("MainPID") or 0)
                else:
                    p = find_proc(procs, c["match"], own)
                    pid = p["pid"] if p else 0
                if pid:
                    pidname[pid] = f"{l['name']} {role}"
        sdrs = usb_sdrs(procs, pidname)
        links = [link_status(l, units_, procs, socks, sdrs, own) for l in cfg["links"]]
        paths = [os.path.dirname(l["db"]["path"]) for l in cfg["links"] if l.get("db", {}).get("path")]
        host = host_info(paths)
        attention = []
        if host["ntp"] is False:
            attention.append({"state": "warn", "what": "Host", "text": "clock not synchronised (NTP): record times across stations will disagree"})
        for d in host["disks"]:
            if d["free"] / d["total"] < 0.1:
                attention.append({"state": "warn", "what": "Host", "text": f"disk at {d['path']} {100 * d['used'] / d['total']:.0f}% full"})
        if host["mem_avail"] / max(1, host["mem_total"]) < 0.1:
            attention.append({"state": "warn", "what": "Host", "text": "memory below 10% available"})
        for l in links:
            attention += [{"state": "bad", "what": l["name"], "text": t} for t in l["problems"]]
            attention += [{"state": "warn", "what": l["name"], "text": t} for t in l["warnings"]]
        for d in sdrs:
            if len(d["holders"]) > 1:
                attention.append({"state": "warn", "what": "SDR", "text": f"{d['kind']} {d['serial']} held by several processes"})
        return {"station": cfg.get("station", host["hostname"]), "now": time.time(), "host": host, "links": links,
                "sdrs": sdrs, "attention": attention,
                "state": "bad" if any(a["state"] == "bad" for a in attention) else "warn" if attention else "ok"}


PAGE = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "page.html"), encoding="utf-8").read()


class Handler(BaseHTTPRequestHandler):
    station = None
    server_version = "station-observatory"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        body = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif path == "/api/status":
            try:
                self._send(200, json.dumps(self.station.status()), "application/json")
            except Exception as e:  # noqa: keep serving
                self._send(500, json.dumps({"error": f"{type(e).__name__}: {e}"}), "application/json")
        elif path == "/healthz":
            self._send(200, "ok", "text/plain")
        else:
            self._send(404, "not found", "text/plain")

    do_HEAD = do_GET


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="Station Observatory: health of every receiver on this station")
    ap.add_argument("--config", default=os.path.join(here, "station.json"))
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--host", default="127.0.0.1", help="address to listen on (default 127.0.0.1: reach it through an SSH tunnel)")
    ap.add_argument("--once", action="store_true", help="print the status as JSON and exit")
    args = ap.parse_args()
    try:
        with open(args.config) as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        sys.exit(f"station-observatory: cannot read {args.config}: {e} (copy station.example.json and edit it)")
    st = Station(cfg)
    if args.once:
        json.dump(st.status(), sys.stdout, indent=1)
        print()
        return
    Handler.station = st
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"station observatory: http://{args.host}:{args.port}/  config={args.config}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
