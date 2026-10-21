"""stress-thermal: ストレステストを流しながら CPU / GPU / SSD などの温度・電力を記録する CLI。

使い方の例:
    uv run stress-thermal --config configs/default.yaml
    uv run stress-thermal --scale 0.02 --only idle-start,cpu-all   # 短縮版で動作確認
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn, TimeRemainingColumn
from rich.table import Table

from .report import aggregate, full_table, key_metrics, markdown_table
from .runner import PhaseRunner, Sampler
from .sensors import SensorSet

REPO = Path(__file__).resolve().parents[2]
console = Console()
log = logging.getLogger("stress_thermal")

# カーネルログから拾うハードウェアエラーのパターン (MCE / WHEA 相当)
HW_ERROR_PATTERN = r"mce:|machine check|hardware error|\[Hardware Error\]|EDAC|corrected error"
# 起動時に毎回出る情報行 (エラーではない) は除外する
HW_ERROR_IGNORE = r"MCE decoding enabled|EDAC MC: Ver:|EDAC .*: (Ver|Giving out device)"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """コマンドライン引数を解釈する。"""
    p = argparse.ArgumentParser(prog="stress-thermal", description="ストレステストを流しながら温度・電力を記録する")
    p.add_argument("--config", type=Path, default=REPO / "configs" / "default.yaml", help="フェーズ定義の YAML")
    p.add_argument("--out", type=Path, help="出力ディレクトリ (既定: runs/<日時>)")
    p.add_argument("--only", help="実行するフェーズ名をカンマ区切りで指定 (既定: 全部)")
    p.add_argument("--scale", type=float, default=1.0, help="全フェーズの時間に掛ける倍率 (動作確認用に 0.02 など)")
    p.add_argument("--interval", type=float, help="サンプリング間隔 [秒] (設定ファイルより優先)")
    p.add_argument("--log", type=Path, help="ログファイル (既定: <出力ディレクトリ>/run.log)")
    p.add_argument("--summarize", type=Path, metavar="RUN_DIR", help="計測せず、既存の出力ディレクトリの集計だけやり直す")
    return p.parse_args(argv)


def setup_logging(log_path: Path) -> None:
    """コンソール (rich) とファイルの両方にログを出す。"""
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(fh)
    log.addHandler(RichHandler(console=console, show_path=False, log_time_format="%H:%M:%S"))


def sh(cmd: str, timeout: float = 30) -> str:
    """シェルコマンドを実行して標準出力を返す (失敗しても例外にしない)。"""
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout, check=False).stdout.strip()
    except subprocess.TimeoutExpired:
        return ""


def nvme_thermal_counters() -> dict[str, dict[str, int]]:
    """各 NVMe の SMART のサーマルスロットリング回数・時間 (sudo -n nvme が使える場合のみ)。"""
    out: dict[str, dict[str, int]] = {}
    for dev in sorted(Path("/dev").glob("nvme[0-9]")):
        try:
            log_json = json.loads(sh(f"sudo -n nvme smart-log {dev} -o json") or "{}")
        except json.JSONDecodeError:
            continue
        # 警告温度・臨界温度の超過時間 [分] と、サーマルスロットリング (T1/T2) の回数・秒数
        keys = ("warning_temp_time", "critical_comp_time", "thm_temp1_trans_count", "thm_temp2_trans_count", "thm_temp1_total_time", "thm_temp2_total_time")
        vals = {k: int(log_json[k]) for k in keys if k in log_json}
        if vals:
            out[dev.name] = vals
    return out


def hw_errors_since(epoch: float | None) -> list[str]:
    """指定時刻以降 (None なら今回の起動以降) のカーネルログからハードウェアエラーの行を拾う。"""
    since = "-b" if epoch is None else f"--since @{int(epoch)}"
    text = sh(f"journalctl -k {since} --no-pager -o short-iso")
    return [ln for ln in text.splitlines() if re.search(HW_ERROR_PATTERN, ln, re.I) and not re.search(HW_ERROR_IGNORE, ln)]


def write_env(run_dir: Path, cfg: dict[str, Any], sensors: SensorSet) -> None:
    """実行環境のスナップショット (BIOS 設定メモ・HW 構成・センサー一覧) を env.md に書く。"""
    dmi = Path("/sys/class/dmi/id")
    rd = lambda n: (dmi / n).read_text().strip() if (dmi / n).exists() else "-"  # noqa: E731
    cpu_model = sh("grep -m1 'model name' /proc/cpuinfo | cut -d: -f2").strip()
    lines = [
        "# 実行環境",
        "",
        f"- 日時: {datetime.now().isoformat(timespec='seconds')}",
        f"- ホスト: {sh('hostname')} / カーネル {sh('uname -r')}",
        f"- マザーボード: {rd('board_vendor')} {rd('board_name')} / BIOS {rd('bios_version')} ({rd('bios_date')})",
        f"- CPU: {cpu_model}",
        f"- cpufreq ドライバ: {sh('cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_driver')} / EPP: {sh('cat /sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference')}",
        f"- GPU: {sh('nvidia-smi --query-gpu=index,name,driver_version,power.limit --format=csv,noheader')}".replace("\n", " / "),
        f"- RAPL (CPU パッケージ電力): {'取得可' if sensors.rapl.available else '取得不可 (energy_uj が root 専用)'}",
        f"- stress-ng: {sh('stress-ng --version')}",
        "",
        "## 設定メモ",
        "",
        str(cfg.get("notes", "")).strip(),
        "",
        "## 記録した hwmon チャンネル",
        "",
        ", ".join(ch.key for ch in sensors.hwmon),
        "",
        "## sensors (開始時)",
        "",
        "```",
        sh("sensors"),
        "```",
    ]
    (run_dir / "env.md").write_text("\n".join(lines) + "\n")


def summarize(run_dir: Path, cfg: dict[str, Any], extra: dict[str, Any] | None = None) -> None:
    """samples.csv を集計し、rich の表を表示して summary.md / summary.json に書く。"""
    limit = float(cfg.get("cpu_temp_limit", 85))
    groups = aggregate(run_dir / "samples.csv", cpu_temp_limit=limit)
    groups.pop("init", None)
    results = json.loads((run_dir / "results.json").read_text()) if (run_dir / "results.json").exists() else []
    verdict: dict[str, str] = {}
    for r in results:
        if not r["ok"]:
            verdict[r["phase"]] = "NG"
        verdict.setdefault(r["phase"], "OK")

    # 主要指標の表 (指標 × フェーズ。フェーズの方が少ないので列にする)
    metrics = {name: key_metrics(g) for name, g in groups.items()}
    cols = list(dict.fromkeys(k for m in metrics.values() for k in m))
    names = list(metrics)
    table = Table(title=f"フェーズ別の最高値 (上限付近 = Tctl ≥ {limit - 1:.0f}℃ だった時間の割合)")
    table.add_column("指標")
    for n in names:
        table.add_column(n, justify="right")
    verdict_row = [verdict.get(n, "-") for n in names]
    table.add_row("結果", *[{"OK": "[green]OK[/]", "NG": "[bold red]NG[/]"}.get(v, v) for v in verdict_row])
    md_rows = [["結果", *verdict_row]]
    for c in cols:
        row = [c] + [metrics[n].get(c, "-") or "-" for n in names]
        table.add_row(*row)
        md_rows.append(row)
    console.print(table)

    # per_core フェーズはコアごとの表も作る
    per_core = {k: g for k, g in aggregate(run_dir / "samples.csv", by_sub=True, cpu_temp_limit=limit).items() if "/cpu" in k}
    core_rows = []
    if per_core:
        ok_by_sub = {f"{r['phase']}/{r['sub']}": ("OK" if r["ok"] else "NG") for r in results if r["sub"]}
        ct = Table(title="コアごとの単スレッド負荷")
        for h in ("コア", "結果", "クロック最高", "Tctl 最高", "CPU 電力最大"):
            ct.add_column(h, justify="right")
        for k, g in per_core.items():
            m = key_metrics(g)
            row = [k.split("/")[-1], ok_by_sub.get(k, "-"), m["CPU クロック最高"], m["Tctl 最高"], m["CPU 電力 最大/平均"].split("/")[0]]
            ct.add_row(*row)
            core_rows.append(row)
        console.print(ct)

    extra = extra or {}
    md = [
        "# ストレステスト結果",
        "",
        markdown_table(["指標"] + names, md_rows),
        "",
    ]
    if core_rows:
        md += ["## コアごとの単スレッド負荷", "", markdown_table(["コア", "結果", "クロック最高", "Tctl 最高", "CPU 電力最大"], core_rows), ""]
    md += ["## ハードウェアエラー (カーネルログ)", ""]
    md += [f"- `{ln}`" for ln in extra.get("hw_errors", [])] or ["- なし"]
    md += ["", "## NVMe のサーマルスロットリング回数 (開始→終了)", ""]
    before, after = extra.get("nvme_before", {}), extra.get("nvme_after", {})
    # 変化した項目だけ出す (全部 0 のままなら「変化なし」)
    for d in sorted(after):
        b, a = before.get(d, {}), after[d]
        diff = [f"{k} {b.get(k)} → {v}" for k, v in a.items() if b.get(k) != v]
        md.append(f"- {d}: " + (", ".join(diff) if diff else "変化なし"))
    if not after:
        md.append("- 取得不可")
    md += ["", "## NG の詳細", ""]
    ng = [r for r in results if not r["ok"]]
    for r in ng:
        for c in r["commands"]:
            if not (c["returncode"] == 0 and not c["fail_lines"]):
                md.append(f"- {r['phase']}/{r['sub']} rc={c['returncode']} `{c['command']}`")
                md += [f"  - `{ln}`" for ln in c["fail_lines"][:5]]
    if not ng:
        md.append("- なし")
    header, rows = full_table(groups)
    md += ["", "## 全センサーの最大値 (平均)", "", markdown_table(header, rows), ""]
    (run_dir / "summary.md").write_text("\n".join(md))
    (run_dir / "summary.json").write_text(json.dumps({"metrics": metrics, "verdict": verdict, **extra}, ensure_ascii=False, indent=2))
    console.print(f"集計: {run_dir / 'summary.md'}")


def status_line(row: dict[str, Any]) -> str:
    """進捗表示用に、最新サンプルから主要な値を1行にまとめる。"""
    def g(k: str, unit: str) -> str:
        v = row.get(k)
        return "-" if v is None else f"{float(v):.0f}{unit}"
    gpus = sorted({k.split("/")[0] for k in row if re.match(r"gpu\d+/temp$", k)})
    nvme = [float(v) for k, v in row.items() if re.match(r"nvme:.*/Composite$", k) and v is not None]
    parts = [
        f"Tctl {g('k10temp/Tctl', '℃')}",
        f"CPU {g('rapl/package-0_w', 'W')} {g('cpu/freq_max_mhz', 'MHz')}",
        *[f"{x.upper()} {g(x + '/temp', '℃')} {g(x + '/power_w', 'W')}" for x in gpus],
        f"NVMe {max(nvme):.0f}℃" if nvme else "NVMe -",
    ]
    fans = [f"{float(v):.0f}" for k, v in sorted(row.items()) if re.match(r"nct\d+/fan\d+$", k) and v]
    if fans:
        parts.append("FAN " + "/".join(fans) + "rpm")
    return " | ".join(parts)


def main(argv: list[str] | None = None) -> int:
    """エントリポイント。"""
    args = parse_args(argv)
    cfg: dict[str, Any] = yaml.safe_load(args.config.read_text())
    if args.interval is not None:
        cfg["interval"] = args.interval

    if args.summarize:
        extra_path = args.summarize / "extra.json"
        extra = json.loads(extra_path.read_text()) if extra_path.exists() else {}
        summarize(args.summarize, cfg, extra)
        return 0

    if args.only:
        names = set(args.only.split(","))
        cfg["phases"] = [p for p in cfg["phases"] if p["name"] in names]
    run_dir = args.out or REPO / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(args.log or run_dir / "run.log")
    # 実際に使った設定を保存 (コマンドライン上書き後)
    (run_dir / "config.yaml").write_text(yaml.safe_dump({**cfg, "scale": args.scale}, allow_unicode=True, sort_keys=False))

    for tool in ("stress-ng", "nvidia-smi", "sensors"):
        if shutil.which(tool) is None:
            log.warning("%s が見つかりません", tool)
    sensors = SensorSet()
    if not sensors.rapl.available:
        log.warning("CPU パッケージ電力 (RAPL) を読めません。読むには: sudo chmod a+r /sys/class/powercap/intel-rapl:*/energy_uj (再起動で戻る)")
    if not any(ch.key.startswith("nct") for ch in sensors.hwmon):
        log.warning("マザーボードのセンサー (nct6775 / nct6683) が未ロードです。ファン回転数・基板温度は記録されません")
    write_env(run_dir, cfg, sensors)

    t_start = time.time()
    extra: dict[str, Any] = {"nvme_before": nvme_thermal_counters(), "hw_errors_boot_before_run": len(hw_errors_since(None))}
    sampler = Sampler(sensors, run_dir / "samples.csv", float(cfg.get("interval", 1.0)))
    sampler.start()

    progress = Progress(
        TextColumn("[bold]{task.description}"), BarColumn(), TextColumn("{task.percentage:>3.0f}%"),
        TimeElapsedColumn(), TextColumn("残り"), TimeRemainingColumn(), TextColumn("{task.fields[status]}"),
        console=console, refresh_per_second=2,
    )
    runner = PhaseRunner(cfg, run_dir, sampler, scale=args.scale)
    plan = runner.plan()
    total = sum(d for _, _, d, _ in plan)
    log.info("出力先: %s (tail -f %s で経過を追える)", run_dir, run_dir / "run.log")
    log.info("予定: %d項目, 合計 %.1f分", len(plan), total / 60)
    for name, sub, dur, cmds in plan:
        log.info("  %-14s %-6s %5d秒  %s", name, sub, dur, " ; ".join(cmds) or "(アイドル)")

    done = 0.0
    last_log = [time.monotonic()]
    aborted = False
    with progress:
        overall = progress.add_task("全体", total=total, status="")
        phase_task = progress.add_task("", total=1, status="")

        def on_tick(label: str, el: float, dur: float) -> None:
            st = status_line(sampler.latest)
            progress.update(phase_task, description=label, completed=el, total=dur, status=st)
            progress.update(overall, completed=done + el, status="")
            # ファイル側にも1分ごとに現在値を残す (無音期間を作らない)
            if time.monotonic() - last_log[0] >= 60:
                last_log[0] = time.monotonic()
                logging.getLogger("stress_thermal.status").info("%s: %s", label, st)

        runner.on_tick = on_tick
        try:
            for name, sub, dur, cmds in plan:
                runner.run_item(name, sub, dur, cmds)
                done += dur
        except KeyboardInterrupt:
            aborted = True
            log.warning("中断されました。負荷コマンドを停止します")
            runner.abort()
    sampler.stop()
    sampler.join(timeout=10)

    extra.update(
        aborted=aborted,
        start=datetime.fromtimestamp(t_start).isoformat(timespec="seconds"),
        end=datetime.now().isoformat(timespec="seconds"),
        hw_errors=hw_errors_since(t_start),
        nvme_after=nvme_thermal_counters(),
    )
    (run_dir / "results.json").write_text(json.dumps(
        [{"phase": r.phase, "sub": r.sub, "start": r.start, "end": r.end, "ok": r.ok,
          "commands": [vars(c) for c in r.commands]} for r in runner.results],
        ensure_ascii=False, indent=2))
    (run_dir / "extra.json").write_text(json.dumps(extra, ensure_ascii=False, indent=2))
    if extra["hw_errors"]:
        log.error("カーネルログにハードウェアエラーが %d 行あります (summary.md 参照)", len(extra["hw_errors"]))
    summarize(run_dir, cfg, extra)
    ok = all(r.ok for r in runner.results) and not extra["hw_errors"] and not aborted
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
