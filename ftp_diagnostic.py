"""FTP diagnostic for the ABB LW Crush Tester.

Standalone tool — no Tk, no parsing, no production code dependency.
Hammers the device with a matrix of FTP strategies and logs raw protocol
metadata (MDTM, SIZE, LIST) alongside payload hashes so you can pinpoint
which layer is serving stale data:

  - If MDTM / SIZE never change while RETR keeps returning the same bytes,
    the device firmware itself is not updating the file.
  - If MDTM / SIZE update but RETR still returns the old payload, the
    transfer layer (or a server-side cache) is the culprit.
  - If different connection strategies (PASV vs active, fresh vs reused)
    give different results, you've isolated the workaround.

Usage:
    python ftp_diagnostic.py                  # interactive menu
    python ftp_diagnostic.py --run-all        # full matrix, then exit
    python ftp_diagnostic.py --run-all --cycles 20 --interval 3

Run it on the lab PC that talks to the tester. Have an operator run a
fresh test partway through the cycle — you want timestamps that bracket
a known machine event.
"""

from __future__ import annotations

import argparse
import ftplib
import hashlib
import os
import socket
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional


DEFAULT_HOST = "192.168.0.3"
DEFAULT_PORT = 21
DEFAULT_USER = "lwuser"
DEFAULT_PASS = "lwapp"
DEFAULT_REMOTE_DIR = "/results"
DEFAULT_FILES = ("sample.xml", "summary.xml")
DEFAULT_TIMEOUT = 10
DEFAULT_INTERVAL = 5.0
DEFAULT_CYCLES = 12

PAYLOAD_DIR = Path("ftp_diagnostic_payloads")
LOG_DIR = Path("ftp_diagnostic_logs")


# ---------------------------------------------------------------------------
#  Connection helpers
# ---------------------------------------------------------------------------

@dataclass
class FTPConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    user: str = DEFAULT_USER
    password: str = DEFAULT_PASS
    remote_dir: str = DEFAULT_REMOTE_DIR
    timeout: int = DEFAULT_TIMEOUT
    files: tuple[str, ...] = DEFAULT_FILES


def open_ftp(cfg: FTPConfig, *, passive: bool, type_binary: bool) -> ftplib.FTP:
    ftp = ftplib.FTP()
    ftp.connect(cfg.host, cfg.port, timeout=cfg.timeout)
    ftp.login(cfg.user, cfg.password)
    ftp.set_pasv(passive)
    if type_binary:
        ftp.voidcmd("TYPE I")
    else:
        ftp.voidcmd("TYPE A")
    ftp.cwd(cfg.remote_dir)
    return ftp


def close_quiet(ftp: Optional[ftplib.FTP]) -> None:
    if ftp is None:
        return
    try:
        ftp.quit()
    except Exception:
        try:
            ftp.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
#  Probes
# ---------------------------------------------------------------------------

@dataclass
class Probe:
    """One observation of one file at one moment in time."""
    timestamp: float
    cycle: int
    strategy: str
    filename: str
    size_cmd: Optional[int] = None        # SIZE response
    mdtm_cmd: Optional[str] = None        # MDTM response (YYYYMMDDhhmmss)
    list_line: Optional[str] = None       # matching LIST line
    bytes_received: Optional[int] = None  # actual payload length
    md5: Optional[str] = None
    elapsed_ms: Optional[float] = None
    error: Optional[str] = None
    payload_path: Optional[str] = None    # saved-to-disk filename


def safe_size(ftp: ftplib.FTP, fn: str) -> Optional[int]:
    try:
        return ftp.size(fn)
    except (*ftplib.all_errors, OSError):
        return None


def safe_mdtm(ftp: ftplib.FTP, fn: str) -> Optional[str]:
    try:
        resp = ftp.voidcmd(f"MDTM {fn}")
        # Response form: "213 YYYYMMDDhhmmss"
        parts = resp.split()
        return parts[-1] if parts else resp
    except (*ftplib.all_errors, OSError):
        return None


def safe_list(ftp: ftplib.FTP, fn: str) -> Optional[str]:
    try:
        lines: list[str] = []
        ftp.retrlines(f"LIST {fn}", lines.append)
        return lines[0] if lines else None
    except (*ftplib.all_errors, OSError):
        return None


def retrieve(ftp: ftplib.FTP, fn: str) -> tuple[Optional[bytes], Optional[str]]:
    try:
        buf = bytearray()
        ftp.retrbinary(f"RETR {fn}", buf.extend)
        return bytes(buf), None
    except (*ftplib.all_errors, OSError) as e:
        return None, f"{type(e).__name__}: {e}"


def md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def save_payload(data: bytes, *, strategy: str, fn: str, cycle: int, ts: float) -> str:
    PAYLOAD_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(ts).strftime("%H%M%S")
    safe_strategy = strategy.replace(" ", "_").replace("/", "-")
    name = f"c{cycle:02d}_{stamp}_{safe_strategy}_{fn}"
    path = PAYLOAD_DIR / name
    path.write_bytes(data)
    return str(path)


# ---------------------------------------------------------------------------
#  Strategies — each is a function that produces one probe per file.
# ---------------------------------------------------------------------------

StrategyFn = Callable[[FTPConfig, int, str, bool], list[Probe]]


def strategy_fresh_pasv_binary(
    cfg: FTPConfig, cycle: int, label: str, save: bool
) -> list[Probe]:
    """Fresh connection, PASV mode, TYPE I — matches current production behavior."""
    return _one_cycle(cfg, cycle, label, save, passive=True, type_binary=True, reuse=False)


def strategy_fresh_active_binary(
    cfg: FTPConfig, cycle: int, label: str, save: bool
) -> list[Probe]:
    """Fresh connection, ACTIVE mode (PORT), TYPE I."""
    return _one_cycle(cfg, cycle, label, save, passive=False, type_binary=True, reuse=False)


def strategy_fresh_pasv_ascii(
    cfg: FTPConfig, cycle: int, label: str, save: bool
) -> list[Probe]:
    """Fresh connection, PASV mode, TYPE A. Embedded servers occasionally
    serve different paths for ASCII vs binary."""
    return _one_cycle(cfg, cycle, label, save, passive=True, type_binary=False, reuse=False)


def strategy_reused_pasv_binary(
    cfg: FTPConfig, cycle: int, label: str, save: bool
) -> list[Probe]:
    """One connection, two RETRs back-to-back. Tests whether repeated reads
    on the same socket get cached."""
    return _one_cycle(cfg, cycle, label, save, passive=True, type_binary=True, reuse=True)


def strategy_double_retr(
    cfg: FTPConfig, cycle: int, label: str, save: bool
) -> list[Probe]:
    """Fresh connection, RETR each file twice in a row to detect intra-session
    caching. Probes are emitted as '<file> [#1]' and '<file> [#2]'."""
    probes: list[Probe] = []
    ftp: Optional[ftplib.FTP] = None
    try:
        ftp = open_ftp(cfg, passive=True, type_binary=True)
        for fn in cfg.files:
            for n in (1, 2):
                p = _probe_one(ftp, cfg, cycle, f"{label} [#{n}]", fn, save)
                probes.append(p)
    except (*ftplib.all_errors, OSError) as e:
        probes.append(Probe(
            timestamp=time.time(), cycle=cycle, strategy=label,
            filename="(connect)", error=f"{type(e).__name__}: {e}",
        ))
    finally:
        close_quiet(ftp)
    return probes


def strategy_post_login_delay(
    cfg: FTPConfig, cycle: int, label: str, save: bool
) -> list[Probe]:
    """Fresh connection, sleep 1.5s between login and RETR. Some embedded
    servers need a moment to refresh their internal file table."""
    probes: list[Probe] = []
    ftp: Optional[ftplib.FTP] = None
    try:
        ftp = open_ftp(cfg, passive=True, type_binary=True)
        time.sleep(1.5)
        for fn in cfg.files:
            probes.append(_probe_one(ftp, cfg, cycle, label, fn, save))
    except (*ftplib.all_errors, OSError) as e:
        probes.append(Probe(
            timestamp=time.time(), cycle=cycle, strategy=label,
            filename="(connect)", error=f"{type(e).__name__}: {e}",
        ))
    finally:
        close_quiet(ftp)
    return probes


def _one_cycle(
    cfg: FTPConfig, cycle: int, label: str, save: bool,
    *, passive: bool, type_binary: bool, reuse: bool,
) -> list[Probe]:
    """Internal: build a list of probes for one connection."""
    probes: list[Probe] = []
    ftp: Optional[ftplib.FTP] = None
    try:
        if reuse:
            ftp = open_ftp(cfg, passive=passive, type_binary=type_binary)
            for fn in cfg.files:
                probes.append(_probe_one(ftp, cfg, cycle, label, fn, save))
        else:
            for fn in cfg.files:
                ftp = open_ftp(cfg, passive=passive, type_binary=type_binary)
                probes.append(_probe_one(ftp, cfg, cycle, label, fn, save))
                close_quiet(ftp)
                ftp = None
    except (*ftplib.all_errors, OSError, socket.error) as e:
        probes.append(Probe(
            timestamp=time.time(), cycle=cycle, strategy=label,
            filename="(connect)", error=f"{type(e).__name__}: {e}",
        ))
    finally:
        close_quiet(ftp)
    return probes


def _probe_one(
    ftp: ftplib.FTP, cfg: FTPConfig, cycle: int,
    label: str, fn: str, save: bool,
) -> Probe:
    ts = time.time()
    probe = Probe(timestamp=ts, cycle=cycle, strategy=label, filename=fn)
    probe.size_cmd = safe_size(ftp, fn)
    probe.mdtm_cmd = safe_mdtm(ftp, fn)
    probe.list_line = safe_list(ftp, fn)
    t0 = time.perf_counter()
    data, err = retrieve(ftp, fn)
    probe.elapsed_ms = (time.perf_counter() - t0) * 1000
    if err:
        probe.error = err
    elif data is not None:
        probe.bytes_received = len(data)
        probe.md5 = md5_hex(data)
        if save:
            probe.payload_path = save_payload(
                data, strategy=label, fn=fn, cycle=cycle, ts=ts)
    return probe


STRATEGIES: dict[str, StrategyFn] = {
    "fresh-pasv-bin":  strategy_fresh_pasv_binary,
    "fresh-act-bin":   strategy_fresh_active_binary,
    "fresh-pasv-asc":  strategy_fresh_pasv_ascii,
    "reused-pasv-bin": strategy_reused_pasv_binary,
    "double-retr":     strategy_double_retr,
    "delayed-login":   strategy_post_login_delay,
}


# ---------------------------------------------------------------------------
#  Reporting
# ---------------------------------------------------------------------------

@dataclass
class Run:
    cfg: FTPConfig
    started: float = field(default_factory=time.time)
    probes: list[Probe] = field(default_factory=list)

    def add(self, probes: list[Probe]) -> None:
        self.probes.extend(probes)
        for p in probes:
            print(format_probe(p))

    def write_log(self) -> Path:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp(self.started).strftime("%Y%m%d_%H%M%S")
        path = LOG_DIR / f"ftp_diag_{stamp}.tsv"
        cols = (
            "iso_time", "cycle", "strategy", "filename",
            "size_cmd", "mdtm_cmd", "bytes_received", "md5",
            "elapsed_ms", "list_line", "payload_path", "error",
        )
        with path.open("w", encoding="utf-8") as f:
            f.write("\t".join(cols) + "\n")
            for p in self.probes:
                row = (
                    datetime.fromtimestamp(p.timestamp).isoformat(timespec="seconds"),
                    str(p.cycle),
                    p.strategy,
                    p.filename,
                    "" if p.size_cmd is None else str(p.size_cmd),
                    p.mdtm_cmd or "",
                    "" if p.bytes_received is None else str(p.bytes_received),
                    p.md5 or "",
                    "" if p.elapsed_ms is None else f"{p.elapsed_ms:.1f}",
                    (p.list_line or "").replace("\t", " "),
                    p.payload_path or "",
                    (p.error or "").replace("\t", " "),
                )
                f.write("\t".join(row) + "\n")
        return path

    def summary(self) -> str:
        if not self.probes:
            return "(no probes recorded)"
        lines = ["", "=" * 78, "SUMMARY", "=" * 78]
        # Group by (strategy, filename) and report distinct values.
        groups: dict[tuple[str, str], list[Probe]] = {}
        for p in self.probes:
            groups.setdefault((p.strategy, p.filename), []).append(p)

        for (strategy, fn), probes in sorted(groups.items()):
            ok = [p for p in probes if p.error is None]
            errs = [p for p in probes if p.error is not None]
            sizes = sorted({p.size_cmd for p in ok if p.size_cmd is not None})
            mdtms = sorted({p.mdtm_cmd for p in ok if p.mdtm_cmd})
            md5s = sorted({p.md5 for p in ok if p.md5})
            byts = sorted({p.bytes_received for p in ok if p.bytes_received is not None})
            lines.append(f"\n  [{strategy}] {fn}")
            lines.append(f"     samples:  {len(probes)} ({len(errs)} errors)")
            lines.append(f"     SIZE:     {sizes or 'n/a'}")
            lines.append(f"     MDTM:     {mdtms or 'n/a'}")
            lines.append(f"     bytes:    {byts or 'n/a'}")
            lines.append(f"     md5s:     {[m[:8] + '…' for m in md5s] or 'n/a'}")
            if len(md5s) == 1 and len(mdtms) > 1:
                lines.append("     ⚠ MDTM changed but content did not — server-side stale data.")
            if len(md5s) > 1 and len(mdtms) == 1 and mdtms[0]:
                lines.append("     ⚠ Content changed but MDTM did not — device clock or MDTM unreliable.")
            if len(md5s) == 1 and len(probes) > 1:
                lines.append("     ⚠ Content NEVER changed across the run.")

        lines.append("")
        lines.append(f"  Total probes:   {len(self.probes)}")
        lines.append(f"  Errors:         {sum(1 for p in self.probes if p.error)}")
        return "\n".join(lines)


def format_probe(p: Probe) -> str:
    t = datetime.fromtimestamp(p.timestamp).strftime("%H:%M:%S")
    if p.error:
        return f"  [{t}] c{p.cycle:02d} {p.strategy:18s} {p.filename:14s} ERROR: {p.error}"
    md5_short = (p.md5 or "")[:8] + "…" if p.md5 else "—"
    size = p.size_cmd if p.size_cmd is not None else "—"
    mdtm = p.mdtm_cmd or "—"
    bts = p.bytes_received if p.bytes_received is not None else "—"
    ms = f"{p.elapsed_ms:.0f}ms" if p.elapsed_ms is not None else "—"
    return (f"  [{t}] c{p.cycle:02d} {p.strategy:18s} {p.filename:14s} "
            f"SIZE={size!s:>6} MDTM={mdtm:>14} bytes={bts!s:>6} "
            f"md5={md5_short} {ms}")


# ---------------------------------------------------------------------------
#  Runners
# ---------------------------------------------------------------------------

def run_matrix(cfg: FTPConfig, cycles: int, interval: float, save: bool) -> Run:
    run = Run(cfg=cfg)
    print(f"\nRunning full strategy matrix: {cycles} cycles, "
          f"{interval}s between cycles, {len(STRATEGIES)} strategies "
          f"× {len(cfg.files)} files = {cycles * len(STRATEGIES) * len(cfg.files)} probes\n")
    print("Have an operator run a fresh test partway through.\n")
    try:
        for cycle in range(1, cycles + 1):
            print(f"--- cycle {cycle}/{cycles} @ {datetime.now().strftime('%H:%M:%S')} ---")
            for label, fn in STRATEGIES.items():
                run.add(fn(cfg, cycle, label, save))
            if cycle < cycles:
                time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[interrupted by user]")
    return run


def run_single(cfg: FTPConfig, strategy: str, cycles: int,
               interval: float, save: bool) -> Run:
    if strategy not in STRATEGIES:
        raise SystemExit(f"unknown strategy {strategy!r}; choose from {list(STRATEGIES)}")
    run = Run(cfg=cfg)
    fn = STRATEGIES[strategy]
    print(f"\nRunning strategy '{strategy}': {cycles} cycles, {interval}s apart\n")
    try:
        for cycle in range(1, cycles + 1):
            print(f"--- cycle {cycle}/{cycles} @ {datetime.now().strftime('%H:%M:%S')} ---")
            run.add(fn(cfg, cycle, strategy, save))
            if cycle < cycles:
                time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[interrupted by user]")
    return run


def run_quick_check(cfg: FTPConfig) -> Run:
    """One probe per strategy. Fast sanity check that the device responds."""
    run = Run(cfg=cfg)
    print(f"\nQuick check: one probe per strategy against {cfg.host}:{cfg.port}\n")
    for label, fn in STRATEGIES.items():
        run.add(fn(cfg, 1, label, save=False))
    return run


# ---------------------------------------------------------------------------
#  CLI / interactive menu
# ---------------------------------------------------------------------------

def interactive(cfg: FTPConfig) -> None:
    while True:
        print("\n" + "=" * 60)
        print(f"  ABB Crush Tester FTP Diagnostic")
        print(f"  Target: {cfg.host}:{cfg.port}  dir={cfg.remote_dir}  user={cfg.user}")
        print("=" * 60)
        print("  1) Quick check (one probe per strategy)")
        print("  2) Run full matrix")
        print("  3) Run a single strategy")
        print("  4) List strategies")
        print("  5) Edit connection settings")
        print("  q) Quit")
        choice = input("\n> ").strip().lower()

        if choice == "q":
            return
        elif choice == "1":
            run = run_quick_check(cfg)
            _wrap_up(run)
        elif choice == "2":
            cycles = _ask_int("cycles", DEFAULT_CYCLES)
            interval = _ask_float("seconds between cycles", DEFAULT_INTERVAL)
            save = _ask_yn("save downloaded payloads to disk?", default=True)
            run = run_matrix(cfg, cycles, interval, save)
            _wrap_up(run)
        elif choice == "3":
            print("\nStrategies:")
            for k in STRATEGIES:
                print(f"  - {k}")
            name = input("strategy name> ").strip()
            cycles = _ask_int("cycles", DEFAULT_CYCLES)
            interval = _ask_float("seconds between cycles", DEFAULT_INTERVAL)
            save = _ask_yn("save downloaded payloads to disk?", default=True)
            try:
                run = run_single(cfg, name, cycles, interval, save)
            except SystemExit as e:
                print(str(e))
                continue
            _wrap_up(run)
        elif choice == "4":
            print("\nStrategies:")
            for k, fn in STRATEGIES.items():
                doc = (fn.__doc__ or "").strip().splitlines()[0]
                print(f"  - {k:18s} {doc}")
        elif choice == "5":
            cfg = _edit_settings(cfg)
        else:
            print("?")


def _wrap_up(run: Run) -> None:
    print(run.summary())
    log = run.write_log()
    print(f"\n  Log:       {log}")
    if any(p.payload_path for p in run.probes):
        print(f"  Payloads:  {PAYLOAD_DIR}/")


def _ask_int(label: str, default: int) -> int:
    raw = input(f"{label} [{default}]> ").strip()
    return int(raw) if raw else default


def _ask_float(label: str, default: float) -> float:
    raw = input(f"{label} [{default}]> ").strip()
    return float(raw) if raw else default


def _ask_yn(label: str, *, default: bool) -> bool:
    suf = "Y/n" if default else "y/N"
    raw = input(f"{label} [{suf}]> ").strip().lower()
    if not raw:
        return default
    return raw.startswith("y")


def _edit_settings(cfg: FTPConfig) -> FTPConfig:
    def ask(label: str, current: str) -> str:
        v = input(f"{label} [{current}]> ").strip()
        return v if v else current

    host = ask("host", cfg.host)
    port_s = ask("port", str(cfg.port))
    user = ask("user", cfg.user)
    password = ask("password", cfg.password)
    remote_dir = ask("remote dir", cfg.remote_dir)
    files_s = ask("files (comma-separated)", ",".join(cfg.files))
    return FTPConfig(
        host=host, port=int(port_s), user=user, password=password,
        remote_dir=remote_dir, timeout=cfg.timeout,
        files=tuple(s.strip() for s in files_s.split(",") if s.strip()),
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--user", default=DEFAULT_USER)
    p.add_argument("--password", default=DEFAULT_PASS)
    p.add_argument("--remote-dir", default=DEFAULT_REMOTE_DIR)
    p.add_argument("--files", default=",".join(DEFAULT_FILES),
                   help="comma-separated filenames")
    p.add_argument("--cycles", type=int, default=DEFAULT_CYCLES)
    p.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    p.add_argument("--strategy", default=None,
                   help=f"run one strategy: {','.join(STRATEGIES)}")
    p.add_argument("--run-all", action="store_true",
                   help="run full matrix non-interactively")
    p.add_argument("--quick", action="store_true",
                   help="one probe per strategy, then exit")
    p.add_argument("--no-save", action="store_true",
                   help="do not save downloaded payloads to disk")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    cfg = FTPConfig(
        host=args.host, port=args.port, user=args.user, password=args.password,
        remote_dir=args.remote_dir, timeout=DEFAULT_TIMEOUT,
        files=tuple(s.strip() for s in args.files.split(",") if s.strip()),
    )
    save = not args.no_save

    if args.quick:
        _wrap_up(run_quick_check(cfg))
    elif args.strategy:
        _wrap_up(run_single(cfg, args.strategy, args.cycles, args.interval, save))
    elif args.run_all:
        _wrap_up(run_matrix(cfg, args.cycles, args.interval, save))
    else:
        interactive(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
