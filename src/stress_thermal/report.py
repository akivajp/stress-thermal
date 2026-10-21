"""記録した CSV からフェーズごとの最高値・平均値を集計し、表にする。"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .sensors import decode_gpu_reasons

META_COLUMNS = ("time", "elapsed_s", "phase", "sub")


@dataclass
class Stat:
    """1列分の集計 (最大・平均・サンプル数・しきい値以上の回数)。"""

    max: float = float("-inf")
    total: float = 0.0
    n: int = 0
    mask_or: int = 0  # ビットマスク列 (GPU のクロック制限理由) の OR

    def add(self, v: float) -> None:
        """値を1つ加える。"""
        self.max = max(self.max, v)
        self.total += v
        self.n += 1

    @property
    def mean(self) -> float:
        """平均値 (サンプルなしなら nan)。"""
        return self.total / self.n if self.n else float("nan")


@dataclass
class GroupStats:
    """1グループ (フェーズ or per_core の1コア) の全列の集計。"""

    columns: dict[str, Stat] = field(default_factory=lambda: defaultdict(Stat))
    n_rows: int = 0
    hot_rows: int = 0  # CPU 温度が上限付近 (cpu_temp_limit - 1 以上) だったサンプル数


def aggregate(csv_path: Path, by_sub: bool = False, cpu_temp_col: str = "k10temp/Tctl", cpu_temp_limit: float = 85.0) -> dict[str, GroupStats]:
    """CSV をフェーズ (by_sub=True なら phase/sub) ごとに集計する。"""
    groups: dict[str, GroupStats] = defaultdict(GroupStats)
    with csv_path.open() as f:
        for row in csv.DictReader(f):
            key = f"{row['phase']}/{row['sub']}" if by_sub and row["sub"] else row["phase"]
            g = groups[key]
            g.n_rows += 1
            for col, raw in row.items():
                if col in META_COLUMNS or raw in ("", None):
                    continue
                v = float(raw)
                g.columns[col].add(v)
                if col.endswith("/clk_reasons"):
                    g.columns[col].mask_or |= int(v)
                if col == cpu_temp_col and v >= cpu_temp_limit - 1:
                    g.hot_rows += 1
    return dict(groups)


def _max_over(g: GroupStats, pattern: str) -> float | None:
    """列名が pattern に一致する列の最大値。該当なしなら None。"""
    vals = [s.max for c, s in g.columns.items() if re.search(pattern, c) and s.n]
    return max(vals) if vals else None


def _fmt(v: float | None, unit: str = "", digits: int = 0) -> str:
    """表示用に整形する (None は '-')。"""
    return "-" if v is None else f"{v:.{digits}f}{unit}"


def key_metrics(g: GroupStats) -> dict[str, str]:
    """主要な指標を表示用の文字列で返す (表の1行分)。"""
    c = g.columns
    out: dict[str, str] = {}
    out["Tctl 最高"] = _fmt(_max_over(g, r"^k10temp/Tctl$"), "℃")
    out["CCD 最高"] = _fmt(_max_over(g, r"^k10temp/Tccd"), "℃")
    out["上限付近の割合"] = f"{100 * g.hot_rows / g.n_rows:.0f}%" if g.n_rows else "-"
    pkg = c.get("rapl/package-0_w")
    out["CPU 電力 最大/平均"] = f"{pkg.max:.0f}/{pkg.mean:.0f}W" if pkg and pkg.n else "-"
    out["CPU クロック最高"] = _fmt(_max_over(g, r"^cpu/freq_max_mhz$"), "MHz")
    for i in sorted({m.group(1) for col in c if (m := re.match(r"gpu(\d+)/", col))}):
        t, w = c.get(f"gpu{i}/temp"), c.get(f"gpu{i}/power_w")
        out[f"GPU{i} 温度/電力"] = f"{_fmt(t.max if t and t.n else None, '℃')}/{_fmt(w.max if w and w.n else None, 'W')}"
        r = c.get(f"gpu{i}/clk_reasons")
        out[f"GPU{i} 制限"] = ",".join(decode_gpu_reasons(r.mask_or)) if r and r.n else "-"
    for col in sorted(x for x in c if re.match(r"nvme:nvme\d+/Composite$", x)):
        out[f"{col.split('/')[0].split(':')[1]} 最高"] = _fmt(c[col].max, "℃")
    out["DIMM 最高"] = _fmt(_max_over(g, r"^spd5118"), "℃")
    # マザーボードのセンサー (nct6775 / nct6683 系がロードされている場合のみ)。
    # サーミスタはどこに付いているか分からないため個別に出す。TSI は CPU 温度の重複なので除く
    for col in sorted(x for x in c if re.match(r"^nct\d+/(Thermistor|SYSTIN|CPUTIN|AUXTIN)", x)):
        out[f"MB {col.split('/')[1]}"] = _fmt(c[col].max, "℃")
    # 回っているファン (最大回転数が 0 より大きいもの) だけ出す
    for col in sorted(x for x in c if re.match(r"^nct\d+/fan\d+$", x)):
        if c[col].n and c[col].max > 0:
            out[f"{col.split('/')[1]} 最高/平均"] = f"{c[col].max:.0f}/{c[col].mean:.0f}rpm"
    return out


def full_table(groups: dict[str, GroupStats]) -> tuple[list[str], list[list[str]]]:
    """全列 × 全グループの「最大 (平均)」の表を返す (ヘッダ, 行)。"""
    names = list(groups)
    cols = sorted({c for g in groups.values() for c in g.columns})
    rows: list[list[str]] = []
    for col in cols:
        cells = [col]
        for n in names:
            s = groups[n].columns.get(col)
            if s is None or not s.n:
                cells.append("-")
            elif col.endswith("/clk_reasons"):
                cells.append(",".join(decode_gpu_reasons(s.mask_or)) or "-")
            else:
                cells.append(f"{s.max:.1f} ({s.mean:.1f})")
        rows.append(cells)
    return ["列"] + names, rows


def markdown_table(header: list[str], rows: list[list[str]]) -> str:
    """Markdown の表を作る。"""
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(lines)
