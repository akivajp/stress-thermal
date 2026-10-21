"""フェーズの実行 (負荷コマンドの起動・停止) と、並行して動くセンサーの記録スレッド。"""

from __future__ import annotations

import csv
import logging
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .sensors import SensorSet, physical_cores

log = logging.getLogger("stress_thermal")

# 負荷コマンドのログから「計算結果の不一致・失敗」を拾う正規表現。
# stress-ng は検証失敗時に "fail:" 行を出し、最後に "failed: N" (N>0) を出す。
FAIL_PATTERN = re.compile(r"fail:|verif\w* fail|failed: *[1-9]|error:|Segmentation|Illegal instruction", re.I)


@dataclass
class CommandResult:
    """1本の負荷コマンドの結果。"""

    command: str
    log_path: str
    returncode: int | None = None
    killed: bool = False  # 時間内に終わらず止めたか
    fail_lines: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """終了コード 0 かつ失敗行なしなら True。"""
        return self.returncode == 0 and not self.fail_lines


@dataclass
class PhaseResult:
    """1フェーズ (per_core の場合は1コア分) の結果。"""

    phase: str
    sub: str
    start: float
    end: float = 0.0
    commands: list[CommandResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """全コマンドが成功したら True (コマンドなしのアイドルも True)。"""
        return all(c.ok for c in self.commands)


class Sampler(threading.Thread):
    """一定間隔でセンサーを読み、CSV に1行ずつ追記するスレッド。

    最新値は `latest` から参照でき、進捗表示に使う。
    """

    def __init__(self, sensors: SensorSet, csv_path: Path, interval: float) -> None:
        super().__init__(daemon=True)
        self.sensors = sensors
        self.csv_path = csv_path
        self.interval = interval
        self.phase = "init"
        self.sub = ""
        self.latest: dict[str, Any] = {}
        self._stop = threading.Event()
        self.t0 = time.monotonic()

    def stop(self) -> None:
        """スレッドを止める (join は呼び出し側で行う)。"""
        self._stop.set()

    def run(self) -> None:
        """記録ループ。列は最初の1行のキーで固定する。"""
        writer: csv.DictWriter[str] | None = None
        with self.csv_path.open("w", newline="") as f:
            next_t = time.monotonic()
            while not self._stop.is_set():
                row: dict[str, Any] = {
                    "time": datetime.now().isoformat(timespec="seconds"),
                    "elapsed_s": round(time.monotonic() - self.t0, 1),
                    "phase": self.phase,
                    "sub": self.sub,
                }
                try:
                    vals = self.sensors.sample()
                except Exception as e:  # センサー読み取りの失敗で記録全体を止めない
                    log.warning("センサー読み取り失敗: %s", e)
                    vals = {}
                row.update({k: (None if v is None else round(v, 3)) for k, v in vals.items()})
                if writer is None:
                    writer = csv.DictWriter(f, fieldnames=list(row), extrasaction="ignore")
                    writer.writeheader()
                writer.writerow(row)
                f.flush()
                self.latest = row
                next_t += self.interval
                self._stop.wait(max(0.0, next_t - time.monotonic()))


def format_command(template: str, **kwargs: Any) -> str:
    """コマンド文字列のプレースホルダ ({duration}, {cpu}, {scratch} など) を埋める。"""
    return template.format(**kwargs)


def scan_fail_lines(log_path: Path, limit: int = 20) -> list[str]:
    """ログから失敗を示す行を最大 limit 行拾う。"""
    try:
        lines = log_path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    return [ln.strip() for ln in lines if FAIL_PATTERN.search(ln)][:limit]


def _kill_group(proc: subprocess.Popen[bytes], sig: int) -> None:
    """プロセスグループごとシグナルを送る (シェル経由で起動した子も止めるため)。"""
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass


class PhaseRunner:
    """設定に従ってフェーズを順に実行する。"""

    def __init__(
        self,
        cfg: dict[str, Any],
        run_dir: Path,
        sampler: Sampler,
        scale: float = 1.0,
        on_tick: Callable[[str, float, float], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.run_dir = run_dir
        self.sampler = sampler
        self.scale = scale  # 動作確認用に全時間を縮める倍率
        self.on_tick = on_tick  # (表示名, フェーズ内経過秒, フェーズ長) を毎秒通知
        self.results: list[PhaseResult] = []
        self.scratch = Path(os.path.expanduser(cfg.get("scratch_dir", "~/.cache/stress-thermal")))
        self.scratch.mkdir(parents=True, exist_ok=True)
        (run_dir / "logs").mkdir(exist_ok=True)
        self._procs: list[subprocess.Popen[bytes]] = []

    def plan(self) -> list[tuple[str, str, int, list[str]]]:
        """(フェーズ名, サブ名, 秒数, コマンド列) のリストに展開する。per_core は1コア1項目。"""
        items: list[tuple[str, str, int, list[str]]] = []
        for ph in self.cfg["phases"]:
            if "per_core" in ph:
                dur = max(1, int(ph["per_core"] * self.scale))
                cores = ph.get("cores") or physical_cores()
                for cpu in cores:
                    items.append((ph["name"], f"cpu{cpu}", dur, [format_command(ph["command"], cpu=cpu, duration=dur, scratch=self.scratch)]))
            else:
                dur = max(1, int(ph["duration"] * self.scale))
                cmds = [format_command(c, duration=dur, scratch=self.scratch, nproc=os.cpu_count()) for c in ph.get("commands", [])]
                items.append((ph["name"], "", dur, cmds))
        return items

    def abort(self) -> None:
        """実行中の負荷コマンドをすべて止める (Ctrl+C 時)。"""
        for p in self._procs:
            _kill_group(p, signal.SIGTERM)
        time.sleep(2)
        for p in self._procs:
            if p.poll() is None:
                _kill_group(p, signal.SIGKILL)

    def run_item(self, phase: str, sub: str, duration: int, commands: list[str]) -> PhaseResult:
        """1項目を実行する。コマンドは duration 秒で自ら終わる前提で、超えたら grace 秒後に止める。"""
        label = f"{phase}/{sub}" if sub else phase
        self.sampler.phase, self.sampler.sub = phase, sub
        res = PhaseResult(phase=phase, sub=sub, start=time.time())
        log.info("開始: %s (%d秒, コマンド%d本)", label, duration, len(commands))
        self._procs = []
        handles = []
        for i, cmd in enumerate(commands):
            safe = re.sub(r"[^\w.-]", "_", label)
            lp = self.run_dir / "logs" / f"{safe}-{i}.log"
            fh = lp.open("wb")
            fh.write(f"$ {cmd}\n".encode())
            fh.flush()
            log.info("  $ %s", cmd)
            p = subprocess.Popen(cmd, shell=True, stdout=fh, stderr=subprocess.STDOUT, start_new_session=True, cwd=Path(__file__).resolve().parents[2])
            self._procs.append(p)
            handles.append(fh)
            res.commands.append(CommandResult(cmd, str(lp)))

        t0 = time.monotonic()
        reported: set[int] = set()
        while (el := time.monotonic() - t0) < duration:
            if self.on_tick:
                self.on_tick(label, el, duration)
            # 予定より早く異常終了したコマンドはすぐ知らせる
            for i, p in enumerate(self._procs):
                if p.poll() not in (None, 0) and i not in reported and el < duration - 5:
                    reported.add(i)
                    log.error("  コマンド%dが %d 秒で異常終了 (rc=%s): %s", i, int(el), p.returncode, commands[i])
            time.sleep(min(1.0, duration - el))

        grace = float(self.cfg.get("grace", 60))
        deadline = time.monotonic() + grace
        for i, p in enumerate(self._procs):
            try:
                p.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                log.warning("  時間内に終わらないため停止: %s", commands[i])
                res.commands[i].killed = True
                _kill_group(p, signal.SIGTERM)
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    _kill_group(p, signal.SIGKILL)
                    p.wait()
            res.commands[i].returncode = p.returncode
        for fh in handles:
            fh.close()
        for c in res.commands:
            c.fail_lines = scan_fail_lines(Path(c.log_path))
            if c.killed:  # 止めたコマンドは終了コードが非0になるが、時間切れ自体は失敗扱いしない
                c.returncode = 0 if not c.fail_lines else c.returncode
        res.end = time.time()
        status = "OK" if res.ok else "NG"
        log.info("終了: %s → %s", label, status)
        for c in res.commands:
            if not c.ok:
                log.error("  NG (rc=%s): %s", c.returncode, c.command)
                for ln in c.fail_lines[:5]:
                    log.error("    %s", ln)
        self.results.append(res)
        return res
