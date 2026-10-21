"""センサーの解釈・失敗検出・集計のテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest

from stress_thermal.report import aggregate, key_metrics
from stress_thermal.runner import FAIL_PATTERN
from stress_thermal.sensors import RaplReader, decode_gpu_reasons, discover_hwmon, parse_nvidia_smi, physical_cores, read_hwmon


def test_parse_nvidia_smi_handles_na_and_hex() -> None:
    """N/A は None、クロック制限理由の16進は整数として読む。"""
    out = parse_nvidia_smi("0, 54, N/A, 38.95, 210, 0, 0, 0x0000000000000024\n1, 46, N/A, 12.48, 210, 30, 0, 0x1\n")
    assert out["gpu0/temp"] == 54
    assert out["gpu0/mem_temp"] is None
    assert out["gpu0/power_w"] == pytest.approx(38.95)
    assert out["gpu0/clk_reasons"] == 0x24
    assert out["gpu1/fan_pct"] == 30


def test_decode_gpu_reasons_ignores_idle() -> None:
    """GpuIdle (0x1) は名前に出さない。"""
    assert decode_gpu_reasons(0x1) == []
    assert decode_gpu_reasons(0x24) == ["SwPowerCap", "SwThermal"]


def _make_hwmon(root: Path, idx: int, name: str, files: dict[str, str]) -> None:
    d = root / f"hwmon{idx}"
    d.mkdir(parents=True)
    (d / "name").write_text(name + "\n")
    for k, v in files.items():
        (d / k).write_text(v + "\n")


def test_discover_and_read_hwmon(tmp_path: Path) -> None:
    """ラベル付きの温度を読み、範囲外の温度は None、HID の電池は除外する。"""
    _make_hwmon(tmp_path, 0, "k10temp", {"temp1_input": "62125", "temp1_label": "Tctl"})
    _make_hwmon(tmp_path, 1, "hid_0003:17EF_battery", {"temp1_input": "30000"})
    _make_hwmon(tmp_path, 2, "nvme", {"temp2_input": "-273150", "temp2_label": "Sensor 1"})
    _make_hwmon(tmp_path, 3, "amdgpu", {"power1_input": "6000", "fan1_input": "1200"})
    chans = discover_hwmon(tmp_path)
    vals = read_hwmon(chans)
    assert vals["k10temp/Tctl"] == pytest.approx(62.125)
    assert not any(k.startswith("hid") for k in vals)
    assert vals["nvme/Sensor 1"] is None
    assert vals["amdgpu/power1"] == pytest.approx(0.006)
    assert vals["amdgpu/fan1"] == 1200


def test_rapl_wraparound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """カウンタが折り返しても正の電力になる。"""
    d = tmp_path / "intel-rapl:0"
    d.mkdir()
    (d / "name").write_text("package-0\n")
    (d / "max_energy_range_uj").write_text("1000000\n")
    (d / "energy_uj").write_text("900000\n")
    times = iter([10.0, 11.0])
    monkeypatch.setattr("stress_thermal.sensors.time.monotonic", lambda: next(times))
    r = RaplReader(root=tmp_path)
    assert r.available
    assert r.read() == {"rapl/package-0_w": None}  # 初回は基準値のみ
    (d / "energy_uj").write_text("100000\n")  # 900000 → 1000000 で折り返して 100000 (差 200000μJ)
    assert r.read()["rapl/package-0_w"] == pytest.approx(0.2)


def test_physical_cores_picks_first_sibling(tmp_path: Path) -> None:
    """SMT の兄弟のうち小さい番号を代表にする。"""
    for cpu, sib in [(0, "0,2"), (1, "1,3"), (2, "0,2"), (3, "1,3")]:
        t = tmp_path / f"cpu{cpu}" / "topology"
        t.mkdir(parents=True)
        (t / "thread_siblings_list").write_text(sib + "\n")
    assert physical_cores(tmp_path) == [0, 1]


@pytest.mark.parametrize(
    ("line", "hit"),
    [
        ("stress-ng: fail:  [1234] cpu: fft verification failed", True),
        ("stress-ng: info:  [1] passed: 32: cpu (16) matrix (16)", False),
        ("stress-ng: info:  [1] failed: 0", False),
        ("stress-ng: info:  [1] failed: 2: cpu (2)", True),
        ("fail: 行列積の結果が基準と一致しない (iter=200)", True),
        ("  read: IOPS=17.9k, BW=17.5GiB/s; err= 0", False),
    ],
)
def test_fail_pattern(line: str, hit: bool) -> None:
    """stress-ng / gpu_load の失敗行だけを拾い、正常行は拾わない。"""
    assert bool(FAIL_PATTERN.search(line)) is hit


def test_aggregate_and_metrics(tmp_path: Path) -> None:
    """フェーズごとの最大・平均と、温度上限付近の割合を出す。"""
    csv_path = tmp_path / "samples.csv"
    csv_path.write_text(
        "time,elapsed_s,phase,sub,k10temp/Tctl,rapl/package-0_w,gpu0/temp,gpu0/power_w,gpu0/clk_reasons,nvme:nvme0/Composite\n"
        "t,0,idle,,50,30,40,20,1,45\n"
        "t,1,load,,84.5,140,70,400,4,60\n"
        "t,2,load,,85,142,72,420,32,62\n"
        "t,3,load,,80,,,,,\n"
    )
    g = aggregate(csv_path, cpu_temp_limit=85)
    assert g["load"].columns["k10temp/Tctl"].max == 85
    assert g["load"].hot_rows == 2
    m = key_metrics(g["load"])
    assert m["CPU 電力 最大/平均"] == "142/141W"
    assert m["GPU0 制限"] == "SwPowerCap,SwThermal"
    assert m["nvme0 最高"] == "62℃"
    assert m["上限付近の割合"] == "67%"


def test_board_sensors_in_metrics(tmp_path: Path) -> None:
    """マザーボードのサーミスタは個別に、ファンは回っているものだけ出す。"""
    csv_path = tmp_path / "samples.csv"
    csv_path.write_text(
        "time,elapsed_s,phase,sub,nct6686/Thermistor 14,nct6686/AMD TSI Addr 98h,nct6686/fan1,nct6686/fan2\n"
        "t,0,load,,61,52,900,0\n"
        "t,1,load,,63,80,1100,0\n"
    )
    m = key_metrics(aggregate(csv_path)["load"])
    assert m["MB Thermistor 14"] == "63℃"
    assert m["fan1 最高/平均"] == "1100/1000rpm"
    assert "fan2 最高/平均" not in m
    assert not any("TSI" in k for k in m)


def test_unconnected_thermistor_is_dropped(tmp_path: Path) -> None:
    """未接続サーミスタの -40℃ は None にする。"""
    _make_hwmon(tmp_path, 0, "nct6686", {"temp3_input": "-40000", "temp3_label": "Thermistor 16"})
    assert read_hwmon(discover_hwmon(tmp_path))["nct6686/Thermistor 16"] is None
