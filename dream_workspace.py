from __future__ import annotations

import json
import random
import time
from pathlib import Path

import lab_foundation as lf


ROOT = lf.ROOT
STRATEGY_DIR = lf.STRATEGY_DIR
ACTIVE_STRATEGY_PATH = lf.ACTIVE_STRATEGY_PATH
EVOLVING_PREFIX = "evolving_agi_v"
MAX_CODE_BYTES = 10 * 1024 * 1024 * 1024  # 10 GB 熔断


def _now() -> str:
    return lf._now()


def _strategy_from_active() -> dict:
    if ACTIVE_STRATEGY_PATH.exists():
        try:
            data = json.loads(ACTIVE_STRATEGY_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {"name": lf.ALGO_BASE, "efficiency": 1.0}


def _score_strategy(efficiency: float, ticks: int = 50) -> tuple[float, float]:
    """在梦境中模拟一个 Subject，返回 (痛觉总量, 存活 tick 数)。"""
    lf.RECORDS_DIR.mkdir(parents=True, exist_ok=True)
    subject = lf.Subject(subject_id=99, cell_count=20)
    subject._efficiency = efficiency  # 直接注入当前候选策略

    pain_before = sum(subject._discovered_pain_map.values())
    alive_ticks = 0
    for _ in range(ticks):
        if subject._dead:
            break
        deltas = [random.randint(-5, 3) for _ in range(len(subject.cells))]
        sensors = {
            "Sensor_A": random.uniform(-10.0, 10.0),
            "Sensor_B": random.uniform(-10.0, 10.0),
            "Sensor_C": random.uniform(-10.0, 10.0),
        }
        subject.apply_environment(deltas)
        subject.ingest_sensors(sensors)
        subject.step()
        alive_ticks += 1

    pain_after = sum(subject._discovered_pain_map.values())
    pain_gain = max(0.0, pain_after - pain_before)
    return pain_gain, float(alive_ticks)


def _search_better_strategy() -> dict | None:
    base = _strategy_from_active()
    base_eff = float(base.get("efficiency", 1.0))
    candidates = sorted({round(base_eff * f, 3) for f in (0.9, 1.0, 1.1, 1.2)})

    best = None
    best_score = None

    for eff in candidates:
        pain, alive = _score_strategy(efficiency=eff, ticks=50)
        # 少痛 + 活得更久 更好
        score = pain - alive * 0.5
        if best is None or score < best_score:
            best = {"name": f"coord_eff_{eff:.3f}", "efficiency": eff}
            best_score = score

    return best


def _total_evolving_size() -> int:
    total = 0
    for p in ROOT.glob(f"{EVOLVING_PREFIX}*.py"):
        try:
            total += p.stat().st_size
        except Exception:
            continue
    return total


def _write_evolving_code(strategy: dict) -> None:
    """将当前策略写出为一个纯数据的 Python 模块（不执行、不自举）。"""
    if _total_evolving_size() >= MAX_CODE_BYTES:
        print(f"[{_now()}] 熔断：evolving_agi_v*.py 总大小超过 10GB，停止写入新版本。")
        return

    v_suffix = int(time.time()) % 1000000
    path = ROOT / f"{EVOLVING_PREFIX}{v_suffix}.py"
    payload = {
        "generated_at": _now(),
        "strategy": strategy,
    }
    code = "CONFIG = " + json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    path.write_text(code, encoding="utf-8")
    print(f"[{_now()}] dream_workspace: 生成新策略代码载体 {path.name}")


def main() -> None:
    print(f"[{_now()}] dream_workspace: 启动梦境演化。")
    STRATEGY_DIR.mkdir(parents=True, exist_ok=True)

    best = _search_better_strategy()
    if best is None:
        print(f"[{_now()}] dream_workspace: 未找到更优策略。")
        return

    ACTIVE_STRATEGY_PATH.write_text(
        json.dumps(best, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"[{_now()}] dream_workspace: 选出新策略 name={best['name']} "
        f"eff={best['efficiency']:.3f}，已写入 active.json。"
    )
    _write_evolving_code(best)


if __name__ == "__main__":
    main()

