"""GPU に行列積の負荷をかけ続ける (gpu-burn の簡易版)。

同じ入力の行列積を繰り返し、最初の結果と一致し続けるかも確かめる (不一致なら "fail:" を出す)。

    uv run --extra gpu python scripts/gpu_load.py --device 0 --duration 600
"""

from __future__ import annotations

import argparse
import time

import torch


def main() -> int:
    """エントリポイント。不一致があれば終了コード 2 を返す。"""
    p = argparse.ArgumentParser(description="GPU に行列積の負荷をかけ続ける")
    p.add_argument("--device", type=int, default=0, help="CUDA デバイス番号")
    p.add_argument("--duration", type=float, default=60, help="負荷をかける秒数")
    p.add_argument("--size", type=int, default=8192, help="正方行列の一辺")
    p.add_argument("--check-every", type=int, default=200, help="何回ごとに結果を照合するか")
    args = p.parse_args()

    torch.manual_seed(0)  # 再現性のため固定
    dev = torch.device(f"cuda:{args.device}")
    a = torch.randn(args.size, args.size, device=dev, dtype=torch.float16)
    b = torch.randn(args.size, args.size, device=dev, dtype=torch.float16)
    ref = a @ b  # 照合用の基準
    torch.cuda.synchronize(dev)
    print(f"device={torch.cuda.get_device_name(dev)} size={args.size} duration={args.duration}s", flush=True)

    flop = 2 * args.size**3
    t0 = last = time.monotonic()
    n = n_last = mismatches = 0
    while time.monotonic() - t0 < args.duration:
        c = a @ b
        n += 1
        if n % args.check_every == 0:
            if not torch.equal(c, ref):
                mismatches += 1
                print(f"fail: 行列積の結果が基準と一致しない (iter={n})", flush=True)
            torch.cuda.synchronize(dev)
            now = time.monotonic()
            if now - last >= 30:  # 30秒ごとに実効性能を出す
                print(f"{now - t0:6.0f}s  {flop * (n - n_last) / (now - last) / 1e12:.1f} TFLOPS", flush=True)
                last, n_last = now, n
    torch.cuda.synchronize(dev)
    print(f"done iters={n} mismatches={mismatches}", flush=True)
    return 2 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
