"""センサー値の読み取り (hwmon / RAPL / CPU 周波数 / nvidia-smi)。

どれも root 権限なしで読めるものだけを使う。読めないもの (RAPL が root 専用の場合など) は
None を返し、呼び出し側で「計測不可」として扱う。
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

HWMON_ROOT = Path("/sys/class/hwmon")
RAPL_ROOT = Path("/sys/class/powercap")
CPU_ROOT = Path("/sys/devices/system/cpu")

# 記録しない hwmon (キーボードの電池残量など、ストレステストと無関係なもの)
SKIP_HWMON_PREFIXES = ("hid_",)

# 温度として明らかにおかしい値を捨てる範囲 (両端を含まない)。
# NVMe の未実装センサーは -273℃ や 65261℃、マザーボードの未接続サーミスタは -40℃ を返す
TEMP_VALID_RANGE = (-40.0, 150.0)

# nvidia-smi で取得する項目 (列名, クエリ名)
NVIDIA_FIELDS: list[tuple[str, str]] = [
    ("temp", "temperature.gpu"),
    ("mem_temp", "temperature.memory"),
    ("power_w", "power.draw"),
    ("sm_mhz", "clocks.sm"),
    ("fan_pct", "fan.speed"),
    ("util_pct", "utilization.gpu"),
    ("clk_reasons", "clocks_event_reasons.active"),
]

# clocks_event_reasons のビット (nvidia-smi のドキュメントより)。0x1 (GpuIdle) は除外して扱う
GPU_CLOCK_REASONS: dict[int, str] = {
    0x2: "AppClocks",
    0x4: "SwPowerCap",
    0x8: "HwSlowdown",
    0x10: "SyncBoost",
    0x20: "SwThermal",
    0x40: "HwThermal",
    0x80: "HwPowerBrake",
    0x100: "DisplayClock",
}


def _read_float(path: Path) -> float | None:
    """sysfs の数値ファイルを読む。読めなければ None。"""
    try:
        return float(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _read_text(path: Path) -> str | None:
    """sysfs のテキストファイルを読む。読めなければ None。"""
    try:
        return path.read_text().strip()
    except OSError:
        return None


@dataclass
class HwmonChannel:
    """記録対象の hwmon の1チャンネル (例: k10temp の Tctl)。"""

    key: str  # CSV の列名 (例: "k10temp/Tctl")
    path: Path  # *_input ファイル
    scale: float  # 生の値に掛ける係数 (温度・電圧は 1/1000, 電力は 1/1e6)
    kind: str  # "temp" / "fan" / "power" / "in"


def hwmon_device_tag(hwmon_dir: Path) -> str:
    """同名の hwmon (nvme×3, spd5118×2 など) を区別するための短いタグを返す。

    NVMe はコントローラ名 (nvme0)、spd5118 は I2C アドレス (2-0051) をそのまま使う。
    """
    name = _read_text(hwmon_dir / "name") or hwmon_dir.name
    dev = (hwmon_dir / "device").resolve().name if (hwmon_dir / "device").exists() else ""
    if name in ("nvme", "spd5118") and dev:
        return f"{name}:{dev}"
    return name


def discover_hwmon(root: Path = HWMON_ROOT) -> list[HwmonChannel]:
    """hwmon 配下の温度・ファン・電力・電圧のチャンネルを列挙する。"""
    channels: list[HwmonChannel] = []
    scales = {"temp": 1e-3, "fan": 1.0, "power": 1e-6, "in": 1e-3}
    for hw in sorted(root.glob("hwmon*"), key=lambda p: int(p.name[5:] or 0)):
        name = _read_text(hw / "name") or ""
        if name.startswith(SKIP_HWMON_PREFIXES):
            continue
        tag = hwmon_device_tag(hw)
        for kind, scale in scales.items():
            # 例: temp1_input, fan2_input, power1_average (amdgpu は power1_input / power1_average)
            for inp in sorted(hw.glob(f"{kind}*_input")):
                idx = inp.name[len(kind) : -len("_input")]
                label = _read_text(hw / f"{kind}{idx}_label") or f"{kind}{idx}"
                channels.append(HwmonChannel(f"{tag}/{label}", inp, scale, kind))
    return channels


def read_hwmon(channels: list[HwmonChannel]) -> dict[str, float | None]:
    """列挙済みチャンネルの現在値を読む。温度の異常値は None にする。"""
    out: dict[str, float | None] = {}
    for ch in channels:
        raw = _read_float(ch.path)
        val = None if raw is None else raw * ch.scale
        if val is not None and ch.kind == "temp" and not (TEMP_VALID_RANGE[0] < val < TEMP_VALID_RANGE[1]):
            val = None
        out[ch.key] = val
    return out


@dataclass
class RaplReader:
    """RAPL (powercap) の積算エネルギーから平均電力 [W] を求める。

    AMD でも intel-rapl という名前で package-0 / core が出る。energy_uj は既定で root 専用のため、
    読めない場合は available=False になる。
    """

    root: Path = RAPL_ROOT
    domains: dict[str, Path] = field(default_factory=dict)  # 列名 -> energy_uj
    max_range: dict[str, float] = field(default_factory=dict)  # 列名 -> 折り返し値
    _last: dict[str, tuple[float, float]] = field(default_factory=dict)  # 列名 -> (時刻, μJ)

    def __post_init__(self) -> None:
        for d in sorted(self.root.glob("intel-rapl:*")):
            name = _read_text(d / "name")
            energy = d / "energy_uj"
            if name is None or _read_float(energy) is None:
                continue  # 権限なし or 存在しない
            key = f"rapl/{name}_w"
            self.domains[key] = energy
            self.max_range[key] = _read_float(d / "max_energy_range_uj") or 2**32

    @property
    def available(self) -> bool:
        """1つでも読めるドメインがあれば True。"""
        return bool(self.domains)

    def read(self) -> dict[str, float | None]:
        """前回呼び出しからの平均電力 [W] を返す (初回は None)。"""
        now = time.monotonic()
        out: dict[str, float | None] = {}
        for key, path in self.domains.items():
            uj = _read_float(path)
            prev = self._last.get(key)
            out[key] = None
            if uj is not None and prev is not None and now > prev[0]:
                delta = uj - prev[1]
                if delta < 0:  # カウンタの折り返し
                    delta += self.max_range[key]
                out[key] = delta / 1e6 / (now - prev[0])
            if uj is not None:
                self._last[key] = (now, uj)
        return out


def physical_cores(root: Path = CPU_ROOT) -> list[int]:
    """物理コアごとに代表の論理 CPU 番号 (SMT の兄弟のうち最小) を返す。"""
    reps: set[int] = set()
    for topo in root.glob("cpu[0-9]*/topology/thread_siblings_list"):
        text = topo.read_text().strip()
        first = text.replace("-", ",").split(",")[0]
        reps.add(int(first))
    return sorted(reps)


def read_cpu_freq(root: Path = CPU_ROOT) -> dict[str, float | None]:
    """全論理 CPU の現在周波数 (scaling_cur_freq) の最大・平均 [MHz] を返す。"""
    freqs = [
        v / 1000
        for p in root.glob("cpu[0-9]*/cpufreq/scaling_cur_freq")
        if (v := _read_float(p)) is not None
    ]
    if not freqs:
        return {"cpu/freq_max_mhz": None, "cpu/freq_avg_mhz": None}
    return {"cpu/freq_max_mhz": max(freqs), "cpu/freq_avg_mhz": sum(freqs) / len(freqs)}


def _parse_nvidia_value(text: str) -> float | None:
    """nvidia-smi の1項目を数値にする。N/A は None、16進 (0x...) は整数として読む。"""
    text = text.strip()
    if not text or "N/A" in text or "Not Supported" in text:
        return None
    try:
        return float(int(text, 16)) if text.startswith("0x") else float(text)
    except ValueError:
        return None


def parse_nvidia_smi(output: str) -> dict[str, float | None]:
    """`nvidia-smi --query-gpu=index,... --format=csv,noheader,nounits` の出力を列名付きの dict にする。"""
    out: dict[str, float | None] = {}
    for line in output.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != len(NVIDIA_FIELDS) + 1:
            continue
        idx = parts[0]
        for (col, _), val in zip(NVIDIA_FIELDS, parts[1:]):
            out[f"gpu{idx}/{col}"] = _parse_nvidia_value(val)
    return out


def read_nvidia() -> dict[str, float | None]:
    """nvidia-smi で全 GPU の値を読む。nvidia-smi が無い・失敗した場合は空 dict。"""
    query = ",".join(["index"] + [q for _, q in NVIDIA_FIELDS])
    try:
        res = subprocess.run(
            ["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return parse_nvidia_smi(res.stdout) if res.returncode == 0 else {}


def decode_gpu_reasons(mask: int) -> list[str]:
    """clocks_event_reasons のビットマスクを名前のリストにする (GpuIdle は除く)。"""
    return [name for bit, name in GPU_CLOCK_REASONS.items() if mask & bit]


class SensorSet:
    """全センサーをまとめて1回分読み取る。"""

    def __init__(self) -> None:
        self.hwmon = discover_hwmon()
        self.rapl = RaplReader()
        self.rapl.read()  # 初回の基準値を取る

    def sample(self) -> dict[str, float | None]:
        """全センサーの現在値を1つの dict で返す。"""
        row: dict[str, float | None] = {}
        row.update(read_hwmon(self.hwmon))
        row.update(self.rapl.read())
        row.update(read_cpu_freq())
        row.update(read_nvidia())
        return row
