# station-observatory

One page for the health of every receiver on a feeder station. The link dashboards
([iridium-observatory](https://github.com/mebrown47/iridium-observatory),
[vdl2-observatory](https://github.com/mebrown47/vdl2-observatory),
[inmarsat-rx](https://github.com/mebrown47/inmarsat-rx)'s Observatory) each show what one receiver
hears; this page shows whether the station is doing its job:

- **Needs attention**: everything that is down or worth a look, worst first.
- **Host**: uptime, load, memory, disk, temperature, and whether the clock is NTP-synchronised
  (record times from different stations only agree when it is).
- **Per link**: the receiver (systemd unit or process: state, uptime, CPU, memory, restarts), its
  SDR and who holds it, its feeds (each destination on the receiver's command line and whether it
  holds a socket to it; UDP gives no delivery confirmation, so an open socket is all there is to
  see), its dashboard and rollup, the newest record in its database and messages per hour for 24 h,
  alerts in its log, and links to its pages.
- **SDRs**: every USB SDR plugged in and which program holds it, so a receiver that is down
  because another program took its SDR says so.

It only reads systemd, /proc, /sys, the journal, logs and the link databases, so it changes
nothing and runs as the receivers' user without sudo. Python 3 stdlib only.

Run it as the user the receivers run as: it can only see the sockets (and so the feeds) of
processes it owns. Reading the receivers' journals needs that user in the `adm` or
`systemd-journal` group, which is the default for the first user on Ubuntu.

```bash
cp station.example.json station.json   # then edit: one entry per link on this station
python3 server.py                       # http://127.0.0.1:8895/
python3 server.py --once                # the status as JSON, for scripts and checks
```

It listens on 127.0.0.1 by default: the page shows command lines, feed destinations and paths,
so reach it through an SSH tunnel (`ssh -L 8895:127.0.0.1:8895 station`). `--host 0.0.0.0` if
you want it on the network.

## Run it as a service

So that it is there after a reboot, run it as a systemd user service (no root, apart from
enabling linger once):

```bash
mkdir -p ~/.config/systemd/user
cp systemd/station-observatory.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now station-observatory.service
sudo loginctl enable-linger "$USER"     # start at boot, without anyone logged in
```

The unit expects the repo in `~/station-observatory`; edit its `ExecStart` line if it is
elsewhere.

## Configuration

`station.example.json` is a working configuration for a station with VDL2, Iridium and
Inmarsat, each set up by its own `install.sh`: the unit names, database paths and ports are the
installers' defaults. Replace the three `sdr` values with your SDRs' serials: the page's SDRs
panel lists every SDR plugged in, with its serial. Each link has:

| Key | |
|---|---|
| `receiver`, `dashboard`, `rollup` | `{"unit": "name.service"}` or `.timer`; `{"match": "text in the command line"}` for a process without a unit; `{"fresh_file": "path", "warn_s": 300}` for a job judged by when it last wrote its log |
| `sdr` | the SDR's USB serial, or part of it |
| `db` | `path`, a `latest` query (newest record time, epoch seconds) and an `hourly` query (`hour, count` from a given hour) |
| `log` | `{"journal": "unit"}` or `{"file": "path"}`, with an optional `pattern` (regex) for lines that count as alerts |
| `pages` | `[{"label", "port", "local"}]`; `local` marks a page served on 127.0.0.1 only |
| `stale_warn_s`, `stale_bad_s` | how old the newest record may get before the link turns amber (900 s) or red (3600 s) |

## Licence

GNU General Public License v3.0 or later. Copyright (c) 2026 Mike Brown.
