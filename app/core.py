"""波形交接台核心业务:CSV 解析、片段入库、有效样点合成、窗口发布。

业务约定:
- 采样率 100 Hz,样点间隔 10 ms;UTC 整 10 秒窗 = 1000 个样点。
- 有效样点按"最高轮次优先"逐样点合成:高轮次只覆盖其重叠位置,
  晚到的低轮次不得覆盖高轮次样点,只能填补空缺。
- 同一(台站,轮次,片段号)同内容重传不重复入库;异内容整批拒收。
- 同轮片段(无论是否同号)重叠位置异值,整批拒收。
- 上传在单个 BEGIN IMMEDIATE 事务中完成,任何失败整体回滚,
  不改变原有片段与窗状态;入库后重算受影响的现役窗,有效样点变化即标 stale。
- 发布需窗内 1000 个有效样点齐全,成功才替换现役窗;失败回滚,保留旧窗状态。
- 同址校时比对:仅用双方现役窗,待核站波形在 ±500 ms 内按 10 ms 滑动,
  以重叠样点的去均值归一化相关系数寻峰;两窗数据与状态取自同一读事务快照。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import re
from dataclasses import dataclass

SAMPLE_MS = 10                              # 100 Hz
WINDOW_MS = 10_000                          # 10 s
WINDOW_SAMPLES = WINDOW_MS // SAMPLE_MS     # 1000
MAX_SAMPLES_PER_SEGMENT = 200_000
MAX_WINDOWS_PER_QUERY = 720
MAX_SHIFT_MS = 500                              # 校时比对:待核站波形滑动范围 ±500 ms
MAX_SHIFT_SAMPLES = MAX_SHIFT_MS // SAMPLE_MS   # 50 个样点
CORR_THRESHOLD = 0.8                            # 可判定的最低相关系数
TIE_TOL = 1e-9                                  # 峰值并列判定容差(绝对)
_INT32_MIN = -(2 ** 31)
_INT32_MAX = 2 ** 31 - 1
_EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)
_INT_TOKEN = re.compile(r"^[+-]?\d+$")
_STATION_TOKEN = re.compile(r"^[\w.\-:]{1,64}$", re.UNICODE)


class BatchRejected(Exception):
    """上传/请求内容不合法或冲突:整批拒收(HTTP 400)。"""


class IncompleteWindow(Exception):
    """发布时窗口有效样点不足 1000(HTTP 409),旧窗状态保持不变。"""

    def __init__(self, filled: int):
        self.filled = filled
        super().__init__(f"filled={filled}")


class WindowConflict(Exception):
    """同轮重叠异值(防御性检查,正常流程在入库前已拦截)。"""


# ---------------------------------------------------------------- 数据模型

@dataclass
class Segment:
    station: str
    start_ms: int
    round: int
    segment_no: int
    samples: list

    @property
    def n(self) -> int:
        return len(self.samples)

    @property
    def end_ms(self) -> int:
        return self.start_ms + self.n * SAMPLE_MS

    def content_hash(self) -> str:
        h = hashlib.sha256()
        h.update(f"{self.start_ms}|".encode())
        h.update(",".join(map(str, self.samples)).encode())
        return h.hexdigest()


def _dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds")


def ms_to_iso(ms: int) -> str:
    return (_EPOCH + _dt.timedelta(milliseconds=ms)).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------- 解析

def parse_time_ms(raw: str) -> int:
    """接受 epoch 毫秒整数或带时区的 ISO-8601(如 2026-11-17T00:00:00.000Z)。"""
    s = str(raw).strip()
    if _INT_TOKEN.match(s):
        return int(s)
    iso = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
    try:
        d = _dt.datetime.fromisoformat(iso)
    except ValueError:
        raise BatchRejected(f"无法解析的UTC时间: {raw!r}")
    if d.tzinfo is None:
        raise BatchRejected(f"时间必须带UTC时区(Z或±hh:mm): {raw!r}")
    delta = d.astimezone(_dt.timezone.utc) - _EPOCH
    if delta.microseconds % 1000:
        raise BatchRejected(f"不支持亚毫秒精度: {raw!r}")
    return delta.days * 86_400_000 + delta.seconds * 1000 + delta.microseconds // 1000


def parse_csv(text: str) -> list:
    """解析 CSV:每行 = 台站,UTC起点,轮次,片段号,振幅序列(空格/分号/逗号分隔)。"""
    segs = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = [f.strip() for f in line.split(",")]
        if len(fields) < 5:
            raise BatchRejected(
                f"第{lineno}行: 至少需要5个字段(台站,UTC起点,轮次,片段号,振幅序列)")
        station, t_raw, round_raw, no_raw = fields[0], fields[1], fields[2], fields[3]
        if not _STATION_TOKEN.match(station):
            raise BatchRejected(f"第{lineno}行: 台站名非法: {station!r}")
        try:
            start_ms = parse_time_ms(t_raw)
        except BatchRejected as e:
            raise BatchRejected(f"第{lineno}行: {e}") from e
        if start_ms % SAMPLE_MS != 0:
            raise BatchRejected(
                f"第{lineno}行: 起点须按10毫秒对齐(得到 {start_ms} ms)")
        if not (_INT_TOKEN.match(round_raw) and _INT_TOKEN.match(no_raw)):
            raise BatchRejected(f"第{lineno}行: 轮次与片段号须为整数")
        rnd, no = int(round_raw), int(no_raw)
        if rnd < 0 or no < 0:
            raise BatchRejected(f"第{lineno}行: 轮次与片段号须为非负整数")
        tokens = [t for t in re.split(r"[,\s;]+", ",".join(fields[4:])) if t]
        if not tokens:
            raise BatchRejected(f"第{lineno}行: 振幅序列为空")
        if not all(_INT_TOKEN.match(t) for t in tokens):
            raise BatchRejected(f"第{lineno}行: 振幅须为整数序列")
        samples = [int(t) for t in tokens]
        if any(v < _INT32_MIN or v > _INT32_MAX for v in samples):
            raise BatchRejected(f"第{lineno}行: 振幅超出 int32 范围")
        if len(samples) > MAX_SAMPLES_PER_SEGMENT:
            raise BatchRejected(
                f"第{lineno}行: 单片样点数超过上限 {MAX_SAMPLES_PER_SEGMENT}")
        segs.append(Segment(station, start_ms, rnd, no, samples))
    if not segs:
        raise BatchRejected("CSV 中没有数据行")
    return segs


# ---------------------------------------------------------------- 冲突校验

def _check_overlap_in_batch(segs: list) -> None:
    """批内同轮片段重叠位置异值 -> 拒收(同值允许)。"""
    groups = {}
    for s in segs:
        groups.setdefault((s.station, s.round), []).append(s)
    for (station, rnd), lst in groups.items():
        seen = {}
        for s in lst:
            for i, v in enumerate(s.samples):
                p = s.start_ms + i * SAMPLE_MS
                prev = seen.get(p)
                if prev is not None and prev != v:
                    raise BatchRejected(
                        f"批内同轮片段重叠异值: 台站{station} 轮次{rnd} "
                        f"位置{p}ms ({prev} != {v})")
                seen[p] = v


def _check_overlap_vs_db(conn, segs: list) -> None:
    """新片段与库中同轮片段重叠位置异值 -> 拒收。"""
    for s in segs:
        rows = conn.execute(
            "SELECT start_ms, n, samples FROM segments "
            "WHERE station=? AND round=? AND start_ms < ? AND start_ms + n*? > ?",
            (s.station, s.round, s.end_ms, SAMPLE_MS, s.start_ms)).fetchall()
        for r in rows:
            db_samples = json.loads(r["samples"])
            db_end = r["start_ms"] + r["n"] * SAMPLE_MS
            p = max(s.start_ms, r["start_ms"])
            hi = min(s.end_ms, db_end)
            while p < hi:
                v_new = s.samples[(p - s.start_ms) // SAMPLE_MS]
                v_db = db_samples[(p - r["start_ms"]) // SAMPLE_MS]
                if v_new != v_db:
                    raise BatchRejected(
                        f"与库中同轮片段重叠异值: 台站{s.station} 轮次{s.round} "
                        f"位置{p}ms ({v_db} != {v_new})")
                p += SAMPLE_MS


# ---------------------------------------------------------------- 有效样点合成

def _fetch_segments(conn, station: str, lo_ms: int, hi_ms: int) -> list:
    rows = conn.execute(
        "SELECT round, start_ms, n, samples FROM segments "
        "WHERE station=? AND start_ms < ? AND start_ms + n*? > ?",
        (station, hi_ms, SAMPLE_MS, lo_ms)).fetchall()
    return [(r["round"], r["start_ms"], r["n"], json.loads(r["samples"])) for r in rows]


def effective_window(segments: list, window_start_ms: int):
    """逐样点取最高轮次,返回 (values, rounds);缺口为 None。"""
    values = [None] * WINDOW_SAMPLES
    rounds = [None] * WINDOW_SAMPLES
    for rnd, start_ms, n, samples in segments:
        base = (start_ms - window_start_ms) // SAMPLE_MS
        for i, v in enumerate(samples):
            pos = base + i
            if 0 <= pos < WINDOW_SAMPLES:
                cur = rounds[pos]
                if cur is None or rnd > cur:
                    rounds[pos] = rnd
                    values[pos] = v
                elif rnd == cur and values[pos] != v:
                    raise WindowConflict(
                        f"同轮重叠异值: 轮次{rnd} 位置{pos} "
                        f"({values[pos]} != {v})")
    return values, rounds


def _gap_ranges(values: list) -> list:
    ranges, start = [], None
    for i, v in enumerate(values):
        if v is None and start is None:
            start = i
        elif v is not None and start is not None:
            ranges.append([start, i - 1])
            start = None
    if start is not None:
        ranges.append([start, len(values) - 1])
    return ranges


# ---------------------------------------------------------------- 上传

def upload_csv(db, text: str) -> dict:
    segs = parse_csv(text)

    # 批内:同轮同号去重;同号异内容 -> 整批拒收
    by_key = {}
    for s in segs:
        key = (s.station, s.round, s.segment_no)
        prev = by_key.get(key)
        if prev is not None and prev.content_hash() != s.content_hash():
            raise BatchRejected(
                f"批内同轮同号内容不一致: 台站{key[0]} 轮次{key[1]} 片段号{key[2]}")
        by_key.setdefault(key, s)
    uniq = list(by_key.values())
    _check_overlap_in_batch(uniq)

    with db.lock:
        conn = db.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            to_insert = []
            for s in uniq:
                row = conn.execute(
                    "SELECT content_hash FROM segments "
                    "WHERE station=? AND round=? AND segment_no=?",
                    (s.station, s.round, s.segment_no)).fetchone()
                if row is None:
                    to_insert.append(s)
                elif row["content_hash"] != s.content_hash():
                    raise BatchRejected(
                        f"与已存片段同轮同号但内容不同: 台站{s.station} "
                        f"轮次{s.round} 片段号{s.segment_no}")
                # 同轮同号同内容:幂等跳过,不重复入库
            _check_overlap_vs_db(conn, to_insert)

            now = _utc_now_iso()
            for s in to_insert:
                conn.execute(
                    "INSERT INTO segments(station,round,segment_no,start_ms,n,"
                    "samples,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (s.station, s.round, s.segment_no, s.start_ms, s.n,
                     _dumps(s.samples), s.content_hash(), now))

            stale = _mark_stale_windows(conn, to_insert)
            conn.commit()
            return {
                "inserted": len(to_insert),
                "skipped": len(segs) - len(to_insert),
                "stale_windows": stale,
            }
        except BaseException:
            conn.rollback()
            raise


def _mark_stale_windows(conn, new_segments: list) -> list:
    """新片段改变了现役窗的有效样点 -> 立即标为过期(stale)。"""
    affected = set()
    for s in new_segments:
        w = s.start_ms - s.start_ms % WINDOW_MS
        while w < s.end_ms:
            affected.add((s.station, w))
            w += WINDOW_MS
    stale = []
    for station, wstart in sorted(affected):
        row = conn.execute(
            "SELECT samples FROM windows "
            "WHERE station=? AND window_start_ms=? AND state='active'",
            (station, wstart)).fetchone()
        if row is None:
            continue
        values, _ = effective_window(
            _fetch_segments(conn, station, wstart, wstart + WINDOW_MS), wstart)
        if _dumps(values) != row["samples"]:
            conn.execute(
                "UPDATE windows SET state='stale' "
                "WHERE station=? AND window_start_ms=?",
                (station, wstart))
            stale.append({"station": station, "window_start_ms": wstart})
    return stale


# ---------------------------------------------------------------- 发布 / 读取

def publish(db, station: str, window_start_ms: int) -> dict:
    if window_start_ms % WINDOW_MS != 0:
        raise BatchRejected("窗口起点须按 UTC 整 10 秒对齐")
    with db.lock:
        conn = db.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            values, rounds = effective_window(
                _fetch_segments(conn, station, window_start_ms,
                                window_start_ms + WINDOW_MS),
                window_start_ms)
            filled = sum(v is not None for v in values)
            if filled < WINDOW_SAMPLES:
                raise IncompleteWindow(filled)  # 回滚,保留旧窗状态
            now = _utc_now_iso()
            row = conn.execute(
                "SELECT version FROM windows WHERE station=? AND window_start_ms=?",
                (station, window_start_ms)).fetchone()
            version = row["version"] + 1 if row else 1
            conn.execute(
                "INSERT INTO windows(station,window_start_ms,state,samples,rounds,"
                "version,published_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(station,window_start_ms) DO UPDATE SET "
                "state='active', samples=excluded.samples, rounds=excluded.rounds, "
                "version=excluded.version, published_at=excluded.published_at",
                (station, window_start_ms, "active", _dumps(values),
                 _dumps(rounds), version, now))
            conn.commit()
            return {"station": station, "window_start_ms": window_start_ms,
                    "start_utc": ms_to_iso(window_start_ms), "state": "active",
                    "version": version, "published_at": now}
        except BaseException:
            conn.rollback()
            raise


def get_picking(db, station: str, window_start_ms: int):
    """拾取接口:仅现役(active)窗可读;stale 窗不再当作现役。"""
    with db.lock:
        row = db.conn.execute(
            "SELECT state, samples, rounds, version, published_at FROM windows "
            "WHERE station=? AND window_start_ms=?",
            (station, window_start_ms)).fetchone()
        if row is None:
            return None
        if row["state"] != "active":
            return "stale"
        return {"station": station, "window_start_ms": window_start_ms,
                "start_utc": ms_to_iso(window_start_ms),
                "state": row["state"], "version": row["version"],
                "published_at": row["published_at"],
                "samples": json.loads(row["samples"]),
                "rounds": json.loads(row["rounds"])}


def list_stations(db) -> list:
    with db.lock:
        rows = db.conn.execute(
            "SELECT station FROM segments UNION SELECT station FROM windows "
            "ORDER BY 1").fetchall()
        return [r["station"] for r in rows]


def list_windows(db, station: str, lo_ms: int, hi_ms: int) -> list:
    with db.lock:
        conn = db.conn
        segs = _fetch_segments(conn, station, lo_ms, hi_ms)
        rows = conn.execute(
            "SELECT window_start_ms, state, version FROM windows "
            "WHERE station=? AND window_start_ms>=? AND window_start_ms<?",
            (station, lo_ms, hi_ms)).fetchall()
        wstate = {r["window_start_ms"]: (r["state"], r["version"]) for r in rows}
        out = []
        w = lo_ms
        while w < hi_ms:
            values, _ = effective_window(segs, w)
            filled = sum(v is not None for v in values)
            st = wstate.get(w)
            out.append({"window_start_ms": w, "start_utc": ms_to_iso(w),
                        "filled": filled, "gaps": WINDOW_SAMPLES - filled,
                        "state": st[0] if st else "none",
                        "version": st[1] if st else 0})
            w += WINDOW_MS
        return out


def window_detail(db, station: str, window_start_ms: int) -> dict:
    with db.lock:
        conn = db.conn
        values, rounds = effective_window(
            _fetch_segments(conn, station, window_start_ms,
                            window_start_ms + WINDOW_MS),
            window_start_ms)
        row = conn.execute(
            "SELECT state, version, published_at FROM windows "
            "WHERE station=? AND window_start_ms=?",
            (station, window_start_ms)).fetchone()
        filled = sum(v is not None for v in values)
        return {"station": station, "window_start_ms": window_start_ms,
                "start_utc": ms_to_iso(window_start_ms),
                "filled": filled, "gaps": WINDOW_SAMPLES - filled,
                "gap_ranges": _gap_ranges(values),
                "state": row["state"] if row else "none",
                "version": row["version"] if row else 0,
                "published_at": row["published_at"] if row else None,
                "samples": values, "rounds": rounds}


# ---------------------------------------------------------------- 同址校时比对

def _mean_removed_corr(x: list, y: list):
    """去均值归一化相关系数;任一侧重叠段零方差时返回 None(该偏移不参与寻峰)。"""
    n = len(x)
    mx = sum(x) / n
    my = sum(y) / n
    sxy = sxx = syy = 0.0
    for a, b in zip(x, y):
        da = a - mx
        db = b - my
        sxy += da * db
        sxx += da * da
        syy += db * db
    if sxx == 0.0 or syy == 0.0:
        return None
    return sxy / math.sqrt(sxx * syy)


def _slide_correlations(ref: list, chk: list) -> list:
    """待核站波形在 ±500 ms 内按 10 ms 步进滑动,逐偏移算去均值归一化相关系数。

    shift_ms > 0 表示把待核站波形向晚移该时长后与参照站对齐,
    即待核站脉冲原本偏早;shift_ms < 0 则待核站偏晚。
    """
    n = len(ref)
    out = []
    for k in range(-MAX_SHIFT_SAMPLES, MAX_SHIFT_SAMPLES + 1):
        lo = max(0, k)
        hi = min(n, n + k)
        corr = _mean_removed_corr(ref[lo:hi], chk[lo - k:hi - k])
        out.append({"shift_ms": k * SAMPLE_MS, "corr": corr})
    return out


def compare_windows(db, ref_station: str, check_station: str,
                    window_start_ms: int) -> dict:
    """同址两台站同一 UTC 整 10 秒窗的校准脉冲比对。

    仅使用双方已发布的现役(active)窗;两窗的状态、版本与样点在同一个
    SQLite 读事务(同一快照)中取出,交错的补传/换版不会把过期或已换版
    的波形混入本次结论。任一侧未发布、过期、常值、峰值并列或最高相关
    系数低于 0.8 时 decidable=False(无法判定)。
    """
    if window_start_ms % WINDOW_MS != 0:
        raise BatchRejected("窗口起点须按 UTC 整 10 秒对齐")
    if ref_station == check_station:
        raise BatchRejected("参照站与待核站不能是同一台站")

    with db.lock:
        conn = db.conn
        # 显式读事务:两个 SELECT 落在同一快照上,即使多实例/多连接并发,
        # 两窗数据与状态也来自同一个已提交状态。
        conn.execute("BEGIN")
        try:
            rows = {}
            for st in (ref_station, check_station):
                rows[st] = conn.execute(
                    "SELECT state, samples, version, published_at FROM windows "
                    "WHERE station=? AND window_start_ms=?",
                    (st, window_start_ms)).fetchone()
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    ref_row, chk_row = rows[ref_station], rows[check_station]

    result = {
        "ref_station": ref_station,
        "check_station": check_station,
        "window_start_ms": window_start_ms,
        "start_utc": ms_to_iso(window_start_ms),
        "ref_version": ref_row["version"] if ref_row else None,
        "check_version": chk_row["version"] if chk_row else None,
        "ref_published_at": ref_row["published_at"] if ref_row else None,
        "check_published_at": chk_row["published_at"] if chk_row else None,
        "max_shift_ms": MAX_SHIFT_MS,
        "corr_threshold": CORR_THRESHOLD,
        "decidable": False,
        "reason_code": None,
        "reason": None,
        "direction": None,       # "早" | "晚" | "同步"(待核站相对参照站)
        "offset_ms": None,       # 有符号时差:正值=待核站偏早,负值=偏晚
        "shift_samples": None,
        "correlation": None,
        "best_shift_ms": None,
        "correlations": None,
    }

    def fail(code: str, reason: str) -> dict:
        result.update(reason_code=code, reason=reason)
        return result

    if ref_row is None:
        return fail("ref_unpublished", f"参照站 {ref_station} 该窗尚未发布")
    if chk_row is None:
        return fail("check_unpublished", f"待核站 {check_station} 该窗尚未发布")
    if ref_row["state"] != "active":
        return fail("ref_stale",
                    f"参照站 {ref_station} 窗口已过期(补传改变了有效样点,需重新发布)")
    if chk_row["state"] != "active":
        return fail("check_stale",
                    f"待核站 {check_station} 窗口已过期(补传改变了有效样点,需重新发布)")

    ref = json.loads(ref_row["samples"])
    chk = json.loads(chk_row["samples"])
    if min(ref) == max(ref):
        return fail("ref_constant", f"参照站 {ref_station} 波形为常值,无法比对")
    if min(chk) == max(chk):
        return fail("check_constant", f"待核站 {check_station} 波形为常值,无法比对")

    corrs = _slide_correlations(ref, chk)
    result["correlations"] = corrs
    defined = [(c["shift_ms"], c["corr"]) for c in corrs if c["corr"] is not None]
    if not defined:
        return fail("no_valid_overlap", "所有偏移下重叠段均无方差,无法计算相关系数")

    best_corr = max(c for _, c in defined)
    peak_shifts = [s for s, c in defined if c >= best_corr - TIE_TOL]
    result["correlation"] = best_corr
    result["best_shift_ms"] = peak_shifts[0]
    if len(peak_shifts) > 1:
        result["tied_shifts_ms"] = peak_shifts
        return fail("peak_tie",
                    f"相关峰并列:偏移 {peak_shifts} ms 处相关系数同为 "
                    f"{best_corr:.6f},无法唯一确定时差")
    if best_corr < CORR_THRESHOLD:
        return fail("low_correlation",
                    f"最高相关系数 {best_corr:.4f} 低于阈值 {CORR_THRESHOLD}")

    shift_ms = peak_shifts[0]
    result.update(
        decidable=True,
        shift_samples=shift_ms // SAMPLE_MS,
        offset_ms=shift_ms,
        direction="同步" if shift_ms == 0 else ("早" if shift_ms > 0 else "晚"),
    )
    return result
