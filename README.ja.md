# stress-thermal

ストレステスト (stress-ng / GPU の行列積 / fio) をフェーズごとに流しながら、
CPU・GPU・NVMe・DIMM などの温度と電力を1秒ごとに記録し、フェーズ別の最高値を集計するツール。
BIOS で電力・電圧を調整した後の安定性と温度の確認用。

## 使い方

```sh
uv sync --extra gpu
uv run stress-thermal --help
uv run stress-thermal --scale 0.02                # 全フェーズを 2% の長さで流す動作確認
uv run stress-thermal                             # 本番 (configs/default.yaml、約2時間)
uv run stress-thermal --only cpu-per-core         # 一部のフェーズだけ
uv run stress-thermal --summarize runs/<日時>     # 集計のやり直し
```

出力は `runs/<日時>/` に置かれる。

| ファイル | 内容 |
|---|---|
| `run.log` | 実行ログ (1分ごとに現在値も書く。`tail -f` で経過を追える) |
| `samples.csv` | 全センサーの時系列 |
| `summary.md` / `summary.json` | フェーズ別の最高値、コアごとの結果、ハードウェアエラー、NVMe のスロットリング回数 |
| `env.md` / `config.yaml` | 実行環境・使った設定 |
| `logs/` | 各負荷コマンドの出力 |

## root 権限が要るセンサー

- **CPU パッケージ電力 (RAPL)**: `energy_uj` が root 専用。計測の間だけ `sudo chmod a+r /sys/class/powercap/intel-rapl:*/energy_uj` で読めるようにする (再起動で元に戻る)。
- **マザーボードのファン・電圧・温度**: `sudo modprobe nct6775`。常時ロードするなら `/etc/modules-load.d/` に書く。
- **NVMe のサーマルスロットリング回数**: `sudo -n nvme smart-log` が使える場合だけ記録する。

## 判定

- 負荷コマンドの終了コードと出力の失敗行 (stress-ng の `--verify` による計算結果の検証、GPU の行列積の照合)
- 実行中のカーネルログの MCE / ハードウェアエラー

どれか1つでもあれば NG (終了コード 1)。

## 計測結果 (Ryzen 9 9950X3D / ASRock X870 Taichi Creator / 空冷)

BIOS で電力上限と Curve Optimizer (CO) を詰めていった記録。各 `runs/<名前>/summary.md` に詳細がある。

| run | BIOS 設定 | 全コア30分の CPU 電力 / Tctl / クロック | ファン | 結果 |
|---|---|---|---|---|
| `20261020-221830` | Performance Preset で温度上限 85℃・CO -30mV、TDP 105W | 183W / 85〜86℃ / 5.0GHz | 1750 / 2070rpm | 全項目 OK (GPU 2枚 + SSD の同時負荷を含む) |
| `20261021-eco105-co40` | 上記 + ECO 105W、CO -40mV (プリセット) | 178W / 86℃ / 5.1GHz | 1750 / 2068rpm | 全コア OK、単コアは中断 |
| `20261021-ppt142-co40` | PBO Limits Manual (PPT 142W) | 176〜178W / 85℃ / 5.1GHz | 1751 / 2070rpm | 約5分で中断 |
| `20261021-preset-auto` | Preset「自動」+ PPT 142W + CO -40 (PBO メニュー) | 127W / 70℃ / 4.6GHz | 1320 / 1500rpm | 全コア OK、**単コア cpu2 でフリーズ** |
| `20261021-ppt142-co20` | PPT 142W + CO -20 | 129W / 70℃ / 4.41GHz | 1325 / 1505rpm | 全コア OK、単コア cpu0〜5 OK (打ち切り) |
| `20261021-ppt162-co25-30` | PPT 162W + CO CCD0 -25 / CCD1 -30 | 141W / 73℃ / 4.66GHz | 1460 / 1670rpm | **全項目 OK** (その後 約51時間の常用でも MCE 0件) |
| `20261024-ppt162-co30-35` | PPT 162W + CO CCD0 -30 / CCD1 -35 | 141W / 71℃ / 4.78GHz | 1371 / 1541rpm | **全項目 OK** |
| `20261024-stock` | 素の状態 (PBO Auto、基準) | 188W / 84℃ / 5.11GHz | 1743 / 2046rpm | 全コア OK (単コアは省略) |
| `20261024-ppt162-co0` | PPT 162W + CO 0 / 0 | 141W / 70℃ / 4.75GHz | 1343 / 1509rpm | 全コア OK (単コアは省略) |

- この BIOS (4.60) では、**Performance Preset が有効だと TDP・ECO モード・PBO Limits の電力上限が効かない**。プリセットを「自動」に戻すと効いた。
- CPU 電力 (RAPL) は TDC に対して 1.16〜1.18 W/A で一定 (110A → 127〜129W、120A → 141W、素の状態の 160A → 188W) で、PPT との比はばらつく。全コア負荷で効いていたのは PPT ではなく TDC とみられる。素の状態でも最初の十数秒だけ PPT 200W に張り付き、その後 188W に下がった。
- 素の状態を 100% とすると、CO -30 / -35 (PPT 162W / TDC 120A) は処理量 (stress-ng の bogo ops/s) が cpu 92.9% / matrix 93.6%、CPU 電力 75%、Tctl -13℃、ファン -370 / -505rpm、電力あたりの処理量 +23〜24%。
- 同じ PPT 162W / TDC 120A で CO 0 / -25・-30 / -30・-35 を比べると、処理量 (cpu / matrix) は 91.5 / 96.0%、90.0 / 93.6%、92.9 / 93.6% で CO の深さの順に並ばない。回ごとのばらつき (±2〜3%) が CO の効果より大きく、電力上限の下での全コア負荷では CO の利点は確認できなかった。
- マザーボードのセンサー (NCT6686D) は `nct6683` ドライバを `force=1` 付きで読み込む必要があった (customer ID `0x1655` がカーネル 7.0 で未知のため)。
