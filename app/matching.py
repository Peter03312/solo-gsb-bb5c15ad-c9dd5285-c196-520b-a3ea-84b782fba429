"""通联日志配对核心算法。

配对规则：
  1. 两条日志必须相互指认（A 本台/对方 == B 对方/本台）；
  2. 波段、模式相同；
  3. 两条带时区时间的绝对时差不超过演练允许时差；
  4. 每条记录最多参与一次配对。

优化目标（字典序优先级）：
  1. 最大化配对数量；
  2. 在配对数最大的前提下最小化总时差；
  3. 仍并列时取字典序最小的配对列表：每对的两个记录号先内部升序，
     再把所有配对整体升序后按列表字典序比较。

锁定（复核员人工互证）：
  ``locked_keys`` 中的记录对强制入选结果，即使因此拉低全场配对数也不
  替换；锁定对占用的记录从自由匹配池中剔除，剩余记录再按上述三级
  目标求解。调用方须保证锁定对合法（现存、相互指认、波段/模式一致、
  时差不超限），否则抛 :class:`InvalidLockError`。

实现方式：
  相互指认关系天然构成二分图——按 ``(min(本台,对方), max(本台,对方),
  波段, 模式)`` 分边，本台呼号较小的记录在左部 L，另一条在右部 R，
  所有候选边只可能跨部存在，因此是二分图。在此图上做最小费用最大流
  （min-cost max-flow, Dijkstra + potential）：
    * 每条流对应一对配对，最大流 => 配对数最大；
    * 费用 = 时差（微秒）* SCALE + OFFSET - 2**(E-1-rank)，
      其中边按配对键 (rec_a, rec_b) 升序赋予 rank 0..E-1，SCALE = 2**E。
      同一配对数下，时差之和的差至少为 SCALE，严格压倒字典序奖励
      （奖励之差的绝对值 <= 2**E - 1），因此最小费用依次满足
      “时差和最小”、“配对列表字典序最小”。OFFSET = 2**(E-1)
      保证所有边费用非负，初始 potential 为 0 即可用 Dijkstra。
"""

from __future__ import annotations

from dataclasses import dataclass
import heapq
from typing import Sequence

# 时差与微秒互相换算。
MICROS_PER_SECOND = 1_000_000


@dataclass(frozen=True)
class Record:
    """一条台站通联日志（算法所需字段）。"""

    record_no: str
    station: str       # 本台呼号（已规范化）
    counterpart: str   # 对方呼号（已规范化）
    ts_micros: int     # 带时区时间归一到 UTC 的微秒时间戳
    band: str          # 波段（已规范化）
    mode: str          # 模式（已规范化）


@dataclass(frozen=True)
class Pair:
    """一对互证成功的日志。"""

    record_a: str
    record_b: str
    time_diff_seconds: float

    def as_tuple(self) -> tuple[str, str]:
        return (self.record_a, self.record_b)


@dataclass(frozen=True)
class MatchResult:
    pairs: tuple[Pair, ...]
    unpaired: tuple[str, ...]
    total_time_diff_seconds: float


class InvalidLockError(ValueError):
    """锁定对在当前日志集合上不再合法（缺失/不互证/波段模式/时差超限）。"""


def _pair_key(r1: Record, r2: Record) -> tuple[str, str]:
    a, b = r1.record_no, r2.record_no
    return (a, b) if a <= b else (b, a)


def pair_eligibility(
    record_map: dict[str, Record], allowed_micros: int, a: str, b: str
) -> tuple[str, str] | None:
    """检查 ``(a, b)`` 是否为可锁定的互证候选。

    合法返回 ``None``；否则返回 ``(原因码, 中文说明)``，原因码取值：
    ``same_record`` / ``record_missing`` / ``not_mutual`` /
    ``band_mode`` / ``time_diff``。
    """
    if a == b:
        return "same_record", f"两条记录号不能相同: {a}"
    ra = record_map.get(a)
    rb = record_map.get(b)
    if ra is None or rb is None:
        missing = a if ra is None else b
        return "record_missing", f"记录不存在: {missing}"
    if ra.station != rb.counterpart or ra.counterpart != rb.station:
        return "not_mutual", f"记录 {a} 与 {b} 未相互指认，不能锁定"
    if ra.band != rb.band or ra.mode != rb.mode:
        return "band_mode", f"记录 {a} 与 {b} 波段或模式不一致，不能锁定"
    diff_micros = abs(ra.ts_micros - rb.ts_micros)
    if diff_micros > allowed_micros:
        return (
            "time_diff",
            f"记录 {a} 与 {b} 时差 {diff_micros / MICROS_PER_SECOND:g} 秒"
            f"超过允许值 {allowed_micros / MICROS_PER_SECOND:g} 秒，不能锁定",
        )
    return None


def _match_free(
    records: Sequence[Record], allowed_micros: int
) -> tuple[list[Pair], set[str], int]:
    """在无锁定记录的集合上求全场最优匹配。

    返回 ``(配对列表, 已配对记录号集合, 总时差微秒)``。
    """
    # --- 构造候选边：用索引加速相互指认的查找 ---
    # 右部索引：(本台, 对方, band, mode) -> [记录...]，
    # 左部记录 r 以 (counterpart, station, band, mode) 反查即可找到
    # 所有“本台==r.对方、对方==r.本台”的候选记录。
    right_index: dict[tuple[str, str, str, str], list[int]] = {}
    left_flags: list[bool] = []
    for i, r in enumerate(records):
        is_left = (r.station, r.counterpart) < (r.counterpart, r.station)
        left_flags.append(is_left)
        if not is_left:
            key = (r.station, r.counterpart, r.band, r.mode)
            right_index.setdefault(key, []).append(i)

    edges: list[tuple[int, int, int, tuple[str, str]]] = []
    for i, r in enumerate(records):
        if not left_flags[i]:
            continue
        for j in right_index.get(
            (r.counterpart, r.station, r.band, r.mode), []
        ):
            diff_micros = abs(records[j].ts_micros - r.ts_micros)
            if diff_micros <= allowed_micros:
                edges.append((i, j, diff_micros, _pair_key(r, records[j])))

    if not edges:
        return [], set(), 0

    # 边按配对键升序定等级：等级越小（记录号组合越小）字典序优先级越高。
    edges.sort(key=lambda e: e[3])
    e_count = len(edges)
    scale = 1 << e_count          # 2**E，时差主项的放大倍数
    # 入选 rank 边的字典序奖励为 2**(E-1-rank)：rank 0 为最高位，
    # 最大化奖励之和等价于最大化“从 rank 0 起的含边掩码”的字典序。
    bonus = [1 << (e_count - 1 - rank) for rank in range(e_count)]
    # 费用 = 时差*SCALE + 偏移 - 奖励；最小化费用即最大化奖励。
    # 最大奖励为 2**(E-1)，减去奖励后该项恒 >= 0，故费用恒非负，
    # 初始 potential 全为 0 即可直接使用 Dijkstra。
    offset = 1 << (e_count - 1)

    left_nodes = sorted({i for (i, _j, _w, _k) in edges})
    right_nodes = sorted({j for (_i, j, _w, _k) in edges})
    lidx = {ri: k + 1 for k, ri in enumerate(left_nodes)}
    ridx = {rj: k + 1 + len(left_nodes) for k, rj in enumerate(right_nodes)}
    source = 0
    sink = 1 + len(left_nodes) + len(right_nodes)
    n_nodes = sink + 1

    # 前向星：每条残余边 [to, cap, cost, rev]
    graph: list[list[list[int]]] = [[] for _ in range(n_nodes)]

    def add_edge(u: int, v: int, cap: int, cost: int) -> int:
        # 返回“u 的前向边”下标。注意：该下标只在之后不再依赖
        # 其他向 u 追加边产生的位置时才稳定——因此每条 L->R 边
        # 追加后立刻记录其下标（向同一 u 继续追加不会改变旧下标）。
        fwd = len(graph[u])
        graph[u].append([v, cap, cost, len(graph[v])])
        graph[v].append([u, 0, -cost, fwd])
        return fwd

    match_arc: list[tuple[int, int]] = []  # 与 edges 对齐：(左部节点, L->R 边下标)
    # 每个左/右节点只连一条容量 1 的源/汇边，保证每条记录最多用一次。
    for u in range(1, 1 + len(left_nodes)):
        add_edge(source, u, 1, 0)
    for k in range(len(right_nodes)):
        v = 1 + len(left_nodes) + k
        add_edge(v, sink, 1, 0)
    for rank, (i, j, weight, _key) in enumerate(edges):
        cost = weight * scale + offset - bonus[rank]
        u = lidx[i]
        v = ridx[j]
        fwd = add_edge(u, v, 1, cost)
        match_arc.append((u, fwd))

    # --- 最小费用最大流（Dijkstra + potential） ---
    potential = [0] * n_nodes
    while True:
        inf = None  # Python 大整数无溢出风险，用 None 表示不可达
        dist = [inf] * n_nodes
        prev_v = [-1] * n_nodes
        prev_e = [-1] * n_nodes
        dist[source] = 0
        heap: list[tuple[int, int]] = [(0, source)]
        while heap:
            d, u = heapq.heappop(heap)
            if d != dist[u]:
                continue
            for ei, arc in enumerate(graph[u]):
                v, cap, cost, _rev = arc
                if cap <= 0:
                    continue
                nd = d + cost + potential[u] - potential[v]
                if dist[v] is None or nd < dist[v]:
                    dist[v] = nd
                    prev_v[v] = u
                    prev_e[v] = ei
                    heapq.heappush(heap, (nd, v))
        if dist[sink] is None:
            break
        for v in range(n_nodes):
            if dist[v] is not None:
                potential[v] += dist[v]
        # 所有容量均为 1，每次增广 1 条流。
        v = sink
        while v != source:
            u = prev_v[v]
            ei = prev_e[v]
            arc = graph[u][ei]
            arc[1] -= 1
            rev = graph[v][arc[3]]
            rev[1] += 1
            v = u

    # --- 提取配对 ---
    pairs: list[Pair] = []
    matched: set[str] = set()
    total_micros = 0
    for rank, (i, j, weight, key) in enumerate(edges):
        # L->R 的前向边容量被用尽 => 该边入选匹配。
        u, fwd = match_arc[rank]
        if graph[u][fwd][1] == 0:
            total_micros += weight
            pairs.append(
                Pair(
                    record_a=key[0],
                    record_b=key[1],
                    time_diff_seconds=weight / MICROS_PER_SECOND,
                )
            )
            matched.add(records[i].record_no)
            matched.add(records[j].record_no)
    return pairs, matched, total_micros


def match_records(
    records: Sequence[Record],
    allowed_diff_seconds: float,
    locked_keys: Sequence[tuple[str, str]] = (),
) -> MatchResult:
    """计算全场最优互证配对。

    ``records`` 内的记录号须唯一（服务层已保证）。``locked_keys`` 中的
    记录对强制入选（服务层已校验合法且互不占用），其记录不参与自由
    匹配；即使锁定使全场配对数下降，锁定对也保留不替换。
    """
    allowed_micros = int(round(allowed_diff_seconds * MICROS_PER_SECOND))
    record_map = {r.record_no: r for r in records}

    # --- 先固定全部锁定对，并把其记录移出自由匹配池 ---
    pairs: list[Pair] = []
    locked_ids: set[str] = set()
    total_micros = 0
    for raw_a, raw_b in locked_keys:
        a, b = (raw_a, raw_b) if raw_a <= raw_b else (raw_b, raw_a)
        problem = pair_eligibility(record_map, allowed_micros, a, b)
        if problem is not None:
            raise InvalidLockError(f"锁定 ({a}, {b}) 已失效: {problem[1]}")
        if a in locked_ids or b in locked_ids:
            raise InvalidLockError(f"记录被多个锁定对重复占用: {a}, {b}")
        locked_ids.add(a)
        locked_ids.add(b)
        weight = abs(record_map[a].ts_micros - record_map[b].ts_micros)
        total_micros += weight
        pairs.append(
            Pair(
                record_a=a,
                record_b=b,
                time_diff_seconds=weight / MICROS_PER_SECOND,
            )
        )

    free_records = [r for r in records if r.record_no not in locked_ids]
    free_pairs, free_matched, free_total = _match_free(
        free_records, allowed_micros
    )
    pairs.extend(free_pairs)
    total_micros += free_total

    pairs.sort(key=lambda p: p.as_tuple())
    matched = locked_ids | free_matched
    unpaired = tuple(
        r.record_no for r in records if r.record_no not in matched
    )
    return MatchResult(
        pairs=tuple(pairs),
        unpaired=unpaired,
        total_time_diff_seconds=total_micros / MICROS_PER_SECOND,
    )
