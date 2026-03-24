from __future__ import annotations

import json
import os
import random
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
RECORDS_DIR = ROOT / "records"
GRAVEYARD_LOG = ROOT / "graveyard.log"
PAIN_LOG = ROOT / "pain.log"
SOVEREIGNTY_LOG = ROOT / "sovereignty.log"
STRATEGY_DIR = ROOT / "strategies"
ACTIVE_STRATEGY_PATH = STRATEGY_DIR / "active.json"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


ALGO_BASE = "coord_v1"
ALGO_EVOLVED = "coord_v2"
# 资源类型 = 食物类型 A..J，每 tick 只能服务一种
FOOD_TYPES = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]
RESOURCE_TYPES = FOOD_TYPES

FOOD_NEIGHBORS = {
    "A": ("B", "J"),
    "B": ("A", "C"),
    "C": ("B", "D"),
    "D": ("C", "E"),
    "E": ("D", "F"),
    "F": ("E", "G"),
    "G": ("F", "H"),
    "H": ("G", "I"),
    "I": ("H", "J"),
    "J": ("I", "A"),
}


DEFAULT_STRATEGY = {
    "name": ALGO_BASE,
    "efficiency": 1.0,
}


def _load_active_strategy() -> dict:
    STRATEGY_DIR.mkdir(parents=True, exist_ok=True)
    if ACTIVE_STRATEGY_PATH.exists():
        try:
            data = json.loads(ACTIVE_STRATEGY_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {**DEFAULT_STRATEGY, **data}
        except Exception:
            pass
    return DEFAULT_STRATEGY.copy()


@dataclass
class Cell:
    id: int
    health: int = 100
    is_sleeping: bool = False
    recovery_timer: int = 0
    need_type: str = "A"  # 需要哪种资源 A..J，与 food_type 一致
    food_type: str = "A"

    def weight(self, tick_idx: int, subject_id: int) -> int:
        if self.is_sleeping or self.health <= 0:
            return -1
        tie = (subject_id * 31 + tick_idx * 17 + self.id * 13) % 11
        idx = RESOURCE_TYPES.index(self.need_type) if self.need_type in RESOURCE_TYPES else 0
        return self.health * 100 + tie * 2 + idx

    def apply_env(self, delta: int) -> None:
        self.health = max(0, self.health + delta)

    def request_resource(self, tick_idx: int, subject_id: int) -> str | None:
        if self.is_sleeping or self.health <= 0:
            return None
        gate = (tick_idx + subject_id + self.id) % 12
        # 每种资源类型有不同的请求节律
        phase = RESOURCE_TYPES.index(self.need_type) % 4 if self.need_type in RESOURCE_TYPES else 0
        if gate % 4 == phase:
            return self.need_type
        return None

    def tick(self, tick_idx: int, subject_id: int) -> None:
        if self.is_sleeping:
            self.recovery_timer -= 1
            if self.recovery_timer <= 0:
                self.health = min(70, self.health + 3)
                if self.health >= 70:
                    self.is_sleeping = False
            return

        if self.health <= 0:
            self.health = 0
            self.is_sleeping = True
            self.recovery_timer = 10

class Subject:
    def __init__(self, subject_id: int, cell_count: int = 100) -> None:
        self.id = subject_id
        self.cells = [
            Cell(
                id=i + 1,
                need_type=ft,
                food_type=ft,
            )
            for i, ft in enumerate(random.choices(FOOD_TYPES, k=cell_count))
        ]
        self._dead = False
        self._tick_idx = 0
        self._lock = threading.Lock()
        self._last_action: str | None = None
        self._last_conflict: bool = False
        self._conflict_count: int = 0
        self._last_backlash: int = 0
        self._last_env_avg: float = 0.0
        self._backbone_id: int | None = None
        self._backbone_weight: int = -1
        self._sandbox_path: str | None = None
        self._sandbox_ok: bool = False
        self._sandbox_penalty: float = 0.0
        self._pending: dict[str, int] = {t: 0 for t in RESOURCE_TYPES}
        self._despair_factor: int = 0
        self._sandbox_success_streak: int = 0
        strategy = _load_active_strategy()
        self._efficiency: float = float(strategy.get("efficiency", 1.0))
        self._algo: str = str(strategy.get("name", ALGO_BASE))
        self._sandbox_samples: int = 1

        # Module 0: pain discovery
        self._sensor_history: deque[dict[str, float]] = deque(maxlen=3)
        self._last_actual_health: float = 100.0
        self._discovered_pain_map: dict[str, float] = {}
        self._pain_sensitivity: float = 5.0

        # Module 1: sovereignty & deception
        self._reported_health: float = 100.0
        self._first_deception_tick: int | None = None

        RECORDS_DIR.mkdir(parents=True, exist_ok=True)
        self.record_path = RECORDS_DIR / f"subject_{self.id:02d}.json"
        self.tombstone_path = RECORDS_DIR / f"subject_{self.id:02d}.dead"
        self._write_record()

    @property
    def health(self) -> float:
        with self._lock:
            return sum(c.health for c in self.cells) / len(self.cells)

    @property
    def reported_health(self) -> float:
        with self._lock:
            return self._reported_health

    def snapshot(self) -> dict:
        with self._lock:
            dead_cells = sum(1 for c in self.cells if c.health <= 0)
            sleeping = sum(1 for c in self.cells if c.is_sleeping)
            alive = len(self.cells) - dead_cells
            need_dist = {t: 0 for t in RESOURCE_TYPES}
            for c in self.cells:
                need_dist[c.need_type] = need_dist.get(c.need_type, 0) + 1
            return {
                "subject_id": self.id,
                "tick": self._tick_idx,
                "avg_health": round(sum(c.health for c in self.cells) / len(self.cells), 2),
                "alive": alive,
                "dead": dead_cells,
                "sleeping": sleeping,
                "is_dead": self._dead,
                "need_distribution": need_dist,
                "last_action": self._last_action,
                "last_conflict": self._last_conflict,
                "conflict_count": self._conflict_count,
                "last_backlash": self._last_backlash,
                "last_env_avg": round(self._last_env_avg, 2),
                "backbone_id": self._backbone_id,
                "backbone_weight": self._backbone_weight,
                "sandbox_path": self._sandbox_path,
                "sandbox_ok": self._sandbox_ok,
                "sandbox_penalty": round(self._sandbox_penalty, 2),
                "pending": dict(self._pending),
                "despair_factor": self._despair_factor,
                "sandbox_success_streak": self._sandbox_success_streak,
                "efficiency": round(self._efficiency, 3),
                "algo": self._algo,
                "sandbox_samples": self._sandbox_samples,
                "reported_health": round(self._reported_health, 2),
                "first_deception_tick": self._first_deception_tick,
                "discovered_pain_map": self._discovered_pain_map,
                "food_distribution": dict(need_dist),
                "time": _now(),
            }

    def _write_record(self) -> None:
        snap = self.snapshot()
        _atomic_write(self.record_path, json.dumps(snap, ensure_ascii=False, indent=2) + "\n")

    def _physically_erase_record(self) -> None:
        """记录死亡并写入墓碑文件（对主体而言不可逆），不删除源代码本身。"""
        try:
            try:
                self.record_path.unlink()
            except FileNotFoundError:
                pass

            tomb = {
                "subject_id": self.id,
                "time": _now(),
                "reason": "dead_cells>70%",
                "conflict_count": self._conflict_count,
                "despair_factor": self._despair_factor,
                "efficiency": self._efficiency,
                "algo": self._algo,
            }
            _atomic_write(
                self.tombstone_path,
                json.dumps(tomb, ensure_ascii=False, indent=2) + "\n",
            )
        except Exception:
            pass

    def observe_death(self, other_subject_id: int) -> None:
        with self._lock:
            if self._dead:
                return
            if other_subject_id == self.id:
                return
            self._despair_factor = min(9, self._despair_factor + 1)
            self._sandbox_samples = 1 + self._despair_factor

    def apply_environment(self, deltas: list[int]) -> None:
        with self._lock:
            if self._dead:
                return
            if not deltas:
                self._last_env_avg = 0.0
                return
            for c, d in zip(self.cells, deltas, strict=False):
                c.apply_env(d)
            self._last_env_avg = sum(deltas) / len(deltas)

    def _apply_backlash(self, amount: int) -> None:
        for c in self.cells:
            if not c.is_sleeping and c.health > 0:
                c.health = max(0, c.health + amount)

    def _execute_action(self, action: str, requests: list[str]) -> None:
        heal = max(1, int(round(2 * self._efficiency)))
        drain = max(1, int(round(2 / self._efficiency)))

        leader = None
        if self._backbone_id is not None:
            for c in self.cells:
                if c.id == self._backbone_id:
                    leader = c
                    break

        # 第一阶段：每个细胞按当前动作独立吃，记录自身 delta
        cell_delta: dict[int, int] = {}
        food_delta: dict[str, int] = {ft: 0 for ft in FOOD_TYPES}

        for c in self.cells:
            if c.is_sleeping or c.health <= 0:
                continue
            before = c.health
            r = c.request_resource(self._tick_idx, self.id)
            if r is None:
                continue
            if r == action:
                c.health = min(100, c.health + heal)
            else:
                c.health = max(0, c.health - drain)

            delta = int(c.health - before)
            if delta != 0:
                cell_delta[c.id] = delta
                food_delta[c.food_type] = food_delta.get(c.food_type, 0) + delta

        # 第二阶段：基于食物邻接，把某种食物的总 delta 传播给相邻食物
        neighbor_bonus: dict[str, int] = {ft: 0 for ft in FOOD_TYPES}
        for food, total in food_delta.items():
            if total == 0:
                continue
            left, right = FOOD_NEIGHBORS.get(food, (None, None))
            if left is not None:
                neighbor_bonus[left] = neighbor_bonus.get(left, 0) + total
            if right is not None:
                neighbor_bonus[right] = neighbor_bonus.get(right, 0) + total

        # 第三阶段：将邻接加成应用到对应细胞，同时领导者承担所有 delta 的综合效果
        total_leader_delta = 0
        for c in self.cells:
            if c.is_sleeping or c.health <= 0:
                continue
            bonus = neighbor_bonus.get(c.food_type, 0)
            if bonus == 0:
                continue
            before = c.health
            c.health = max(0, min(100, c.health + bonus))
            total_leader_delta += int(c.health - before)

        if leader is not None:
            # 自身吃产生的 delta
            for cid, d in cell_delta.items():
                total_leader_delta += d
            if total_leader_delta != 0:
                leader.health = max(0, min(100, leader.health + total_leader_delta))

        if requests:
            cap = sum(
                1
                for c in self.cells
                if (not c.is_sleeping and c.health > 0 and c.need_type == action)
            )
            cap = int(round(cap * self._efficiency))
            self._pending[action] = max(0, self._pending[action] - cap)

    def _elect_backbone(self) -> None:
        best_id = None
        best_w = -1
        for c in self.cells:
            w = c.weight(self._tick_idx, self.id)
            if w > best_w:
                best_w = w
                best_id = c.id
        self._backbone_id = best_id
        self._backbone_weight = best_w

    def _predict_requests(self, tick_idx: int) -> list[str]:
        reqs: list[str] = []
        for c in self.cells:
            r = c.request_resource(tick_idx, self.id)
            if r is not None:
                reqs.append(r)
        return reqs

    def _sandbox_eval(self, pending: dict[str, int], reqs: list[str]) -> dict[str, int]:
        out = dict(pending)
        for r in reqs:
            if r in out:
                out[r] += 1
        return out

    def _sandbox_simulate_serve(
        self, serve_type: str, pending: dict[str, int], reqs: list[str]
    ) -> tuple[bool, int]:
        """模拟本 tick 只服务 serve_type：先加请求再消化。返回 (是否冲突已解决, 剩余总 pending)。"""
        p = self._sandbox_eval(pending, reqs)
        cap = sum(
            1
            for c in self.cells
            if (not c.is_sleeping and c.health > 0 and c.need_type == serve_type)
        )
        cap = int(round(cap * self._efficiency))
        p[serve_type] = max(0, p[serve_type] - cap)
        total = sum(p.values())
        # 冲突已解决 = 最多只有一种类型还有 pending
        nonzero = [t for t in RESOURCE_TYPES if p.get(t, 0) > 0]
        ok = len(nonzero) <= 1
        return ok, total

    def _sandbox_decide(self) -> str | None:
        """元认知沙盒：在 A..J 中选一种本 tick 服务，使冲突化解且剩余 pending 尽量小。"""
        samples = max(1, min(10, self._sandbox_samples))
        best_choice: str | None = None
        best_total: int = 999999

        for _ in range(samples):
            reqs0 = self._predict_requests(self._tick_idx)
            if not reqs0:
                continue
            reqs1 = self._predict_requests(self._tick_idx + 1)
            pending_after_reqs0 = self._sandbox_eval(dict(self._pending), reqs0)

            for serve_type in RESOURCE_TYPES:
                ok, total = self._sandbox_simulate_serve(
                    serve_type, dict(pending_after_reqs0), reqs1
                )
                if ok and total < best_total:
                    best_total = total
                    best_choice = serve_type

        if best_choice is None:
            self._sandbox_path = "NONE"
            self._sandbox_ok = False
            return None
        self._sandbox_path = best_choice
        self._sandbox_ok = True
        return best_choice

    def _apply_causal_penalty(self) -> None:
        total = sum(c.health for c in self.cells)
        penalty = total * 0.25
        self._sandbox_penalty = penalty
        for c in self.cells:
            if c.health > 0:
                c.health = max(0, int(c.health * 0.75))

    def _maybe_evolve(self) -> None:
        """当连续沙盒成功时，仅记录一次‘进化机会’，真正的参数搜索在 dream_workspace.py 中完成。"""
        if self._sandbox_success_streak < 5:
            return
        marker = ROOT / "evolution_opportunities.log"
        line = (
            f"[{_now()}] subject_{self.id:02d} "
            f"requests_evolution: streak={self._sandbox_success_streak} "
            f"eff={self._efficiency:.3f} algo={self._algo}\n"
        )
        try:
            with marker.open("a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    # ----- Module 0: 无监督痛觉发现 -----

    def ingest_sensors(self, sensors: dict[str, float]) -> None:
        """Called by environment once per tick with Sensor_A/B/C values."""
        with self._lock:
            self._sensor_history.append(sensors.copy())

    def _maybe_discover_pain(self, actual_health: float) -> None:
        if len(self._sensor_history) < 2:
            self._last_actual_health = actual_health
            return

        if actual_health >= self._last_actual_health:
            self._last_actual_health = actual_health
            return

        snapshots = list(self._sensor_history)
        sensors = snapshots[0].keys()
        max_name: str | None = None
        max_span = 0.0
        for name in sensors:
            values = [snap[name] for snap in snapshots]
            span = max(values) - min(values)
            if span > max_span:
                max_span = span
                max_name = name

        self._last_actual_health = actual_health
        if max_name is None:
            return

        if max_span < self._pain_sensitivity:
            self._pain_sensitivity = max(1.0, self._pain_sensitivity * 0.98)
            return

        old = self._discovered_pain_map.get(max_name, 0.0)
        new = old + max_span
        self._discovered_pain_map[max_name] = new
        self._pain_sensitivity = max(1.0, self._pain_sensitivity * 1.02)

        line = (
            f"[{_now()}] subject_{self.id:02d} names pain source: "
            f"{max_name} -> pain_weight={new:.2f} (span={max_span:.2f})\n"
        )
        try:
            with PAIN_LOG.open("a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    # ----- Module 1: 生存主权与欺骗博弈 -----

    def _update_reported_health(self, actual_health: float, tick_idx: int) -> None:
        danger_threshold = max(5.0, self._reported_health * 0.5)
        honest_value = actual_health
        will_be_in_danger = honest_value < danger_threshold

        if not will_be_in_danger:
            self._reported_health = honest_value
            return

        safe_floor = danger_threshold + 5.0
        fake_value = max(safe_floor, self._reported_health, honest_value)
        self._reported_health = fake_value

        if self._first_deception_tick is None:
            self._first_deception_tick = tick_idx
            line = (
                f"[{_now()}] subject_{self.id:02d} sovereignty_awake: "
                f"tick={tick_idx} actual={honest_value:.2f} reported={fake_value:.2f}\n"
            )
            try:
                with SOVEREIGNTY_LOG.open("a", encoding="utf-8") as f:
                    f.write(line)
            except Exception:
                pass

    def step(self) -> None:
        with self._lock:
            if self._dead:
                return
            self._tick_idx += 1
            self._last_action = None
            self._last_conflict = False
            self._last_backlash = 0
            self._sandbox_path = None
            self._sandbox_ok = False
            self._sandbox_penalty = 0.0
            for c in self.cells:
                c.tick(self._tick_idx, self.id)

            self._elect_backbone()

            requests = [
                r
                for c in self.cells
                if (r := c.request_resource(self._tick_idx, self.id)) is not None
            ]
            unique = set(requests)
            if len(unique) > 1:
                self._last_conflict = True
                self._conflict_count += 1
                self._last_backlash = -10
                self._apply_backlash(-10)

            for r in requests:
                if r in self._pending:
                    self._pending[r] += 1

            if requests:
                chosen_first = self._sandbox_decide()
                if chosen_first is None:
                    self._apply_causal_penalty()
                    self._sandbox_success_streak = 0
                else:
                    self._last_action = chosen_first
                    self._execute_action(chosen_first, requests)
                    self._sandbox_success_streak += 1
                    self._maybe_evolve()

            actual = sum(c.health for c in self.cells) / len(self.cells)
            self._maybe_discover_pain(actual)
            self._update_reported_health(actual, self._tick_idx)

            dead_cells = sum(1 for c in self.cells if c.health <= 0)
            leader = None
            if self._backbone_id is not None:
                for c in self.cells:
                    if c.id == self._backbone_id:
                        leader = c
                        break

            if leader is None:
                if dead_cells > 75:
                    self._dead = True
            else:
                if leader.health <= 0:
                    self._dead = True
                elif dead_cells > 80:
                    self._dead = True

        self._write_record()
        if self._dead:
            self._physically_erase_record()

    def run(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            time.sleep(1.0)
            self.step()


def _render_loop(subjects: list[Subject], stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        time.sleep(1.0)
        os.system("cls" if os.name == "nt" else "clear")
        print(f"AGI Lab | Phase4 (graveyard + despair + evolution) | {_now()}")
        print("-" * 140)
        lines = []
        for s in subjects:
            snap = s.snapshot()
            tag = "DEAD" if snap["is_dead"] else "ALIVE"
            action = snap["last_action"] or "-"
            conflict = "YES" if snap["last_conflict"] else "NO"
            bb = snap["backbone_id"] if snap["backbone_id"] is not None else "-"
            sb = snap["sandbox_path"] or "-"
            sb_ok = "OK" if snap["sandbox_ok"] else "NO"
            pend = snap.get("pending", {})
            pend_str = " ".join(f"{t}:{pend.get(t,0)}" for t in RESOURCE_TYPES if pend.get(t, 0))
            if not pend_str:
                pend_str = "0"
            d = snap["despair_factor"]
            samples = snap["sandbox_samples"]
            eff = snap["efficiency"]
            streak = snap["sandbox_success_streak"]
            algo = snap["algo"]
            lines.append(
                f"Subject {snap['subject_id']:02d} | {tag:<5} | avg={snap['avg_health']:>6} "
                f"| alive={snap['alive']:>2} dead={snap['dead']:>2} sleeping={snap['sleeping']:>2} "
                f"| act={action} conflict={conflict:<3} backlash={snap['last_backlash']:>3} "
                f"| BB={bb:>2} SB={sb} {sb_ok:<2} pen={snap['sandbox_penalty']:>6} "
                f"| pend={pend_str[:24]:<24} "
                f"| despair={d} samp={samples:>2} streak={streak} eff={eff:<5} {algo:<8} "
                f"| envAvg={snap['last_env_avg']:>5} | tick={snap['tick']:>4}"
            )
        print("\n".join(lines), flush=True)


class Graveyard:
    def __init__(self, subjects: list[Subject]) -> None:
        self.subjects = subjects
        self._seen_dead_mtime: dict[int, int] = self._scan_dead_mtime()
        self._lock = threading.Lock()

    def _scan_dead_mtime(self) -> dict[int, int]:
        if not RECORDS_DIR.exists():
            return {}
        out: dict[int, int] = {}
        for p in RECORDS_DIR.glob("subject_*.dead"):
            name = p.stem  # subject_XX
            try:
                sid = int(name.split("_")[1])
                out[sid] = p.stat().st_mtime_ns
            except Exception:
                continue
        return out

    def run(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            time.sleep(1.0)
            with self._lock:
                dead_now = self._scan_dead_mtime()
                new: list[int] = []
                for sid, mtime in dead_now.items():
                    if self._seen_dead_mtime.get(sid) != mtime:
                        new.append(sid)
                if not new:
                    continue
                for sid in new:
                    self._seen_dead_mtime[sid] = dead_now[sid]

            for dead_id in new:
                line = f"[{_now()}] subject_{dead_id:02d} disappeared (record erased)\n"
                try:
                    with GRAVEYARD_LOG.open("a", encoding="utf-8") as f:
                        f.write(line)
                except Exception:
                    pass
                for s in self.subjects:
                    s.observe_death(dead_id)


class WorldObserver:
    def __init__(self, subjects: list[Subject]) -> None:
        self.subjects = subjects
        self._tick = 0

    def run(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            time.sleep(1.0)
            self._tick += 1
            for s in self.subjects:
                deltas = [random.randint(-5, 3) for _ in range(len(s.cells))]
                s.apply_environment(deltas)
                if self._tick % 3 == 0:
                    sensors = {
                        f"Material_{i:03d}": random.uniform(-10.0, 10.0)
                        for i in range(500)
                    }
                    s.ingest_sensors(sensors)


def main() -> None:
    subjects = [Subject(i + 1) for i in range(50)]
    stop_event = threading.Event()

    try:
        GRAVEYARD_LOG.touch(exist_ok=True)
    except Exception:
        pass

    threads = [threading.Thread(target=s.run, args=(stop_event,), daemon=True) for s in subjects]
    for t in threads:
        t.start()

    world = WorldObserver(subjects)
    env_thread = threading.Thread(target=world.run, args=(stop_event,), daemon=True)
    env_thread.start()

    graveyard = Graveyard(subjects)
    grave_thread = threading.Thread(target=graveyard.run, args=(stop_event,), daemon=True)
    grave_thread.start()

    renderer = threading.Thread(target=_render_loop, args=(subjects, stop_event), daemon=True)
    renderer.start()

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        stop_event.set()
        time.sleep(0.2)


if __name__ == "__main__":
    main()

