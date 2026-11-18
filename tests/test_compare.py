"""同址校时比对自测:方向/时差、未发布、过期、常值、并列峰、低相关、
换版后结论更新、补传/换版与比对交错时的快照一致性。"""
import random
import threading

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

W0 = 1_700_000_000_000  # 整 10 秒对齐的窗起点(epoch 毫秒)
REF, CHK = "REF1", "CHK1"
PULSE = [2, -5, 8, -3, 1]  # 非对称脉冲,避免偶然并列峰


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def wave(pulse_at=None):
    """1000 样点窗口,脉冲起于 pulse_at(样点索引)。"""
    w = [0] * 1000
    if pulse_at is not None:
        for i, v in enumerate(PULSE):
            w[pulse_at + i] = v
    return w


def row(station, start_ms, rnd, no, samples):
    return f"{station},{start_ms},{rnd},{no},{' '.join(map(str, samples))}"


def upload(client, text):
    return client.post("/api/upload", content=text)


def publish(client, station, wstart=W0):
    return client.post("/api/publish",
                       json={"station": station, "window_start_ms": wstart})


def publish_pair(client, ref_pulse, chk_pulse, rnd, wstart=W0):
    """上传两站整窗波形(脉冲位置各异)并双双发布。"""
    text = "\n".join([
        row(REF, wstart, rnd, 0, wave(ref_pulse)),
        row(CHK, wstart, rnd, 0, wave(chk_pulse)),
    ])
    r = upload(client, text)
    assert r.status_code == 200, r.text
    for st in (REF, CHK):
        p = publish(client, st, wstart)
        assert p.status_code == 200, p.text


def compare(client, ref=REF, chk=CHK, wstart=W0):
    return client.get(f"/api/compare?ref={ref}&check={chk}&window_start_ms={wstart}")


# ---------------------------------------------------------------- 方向与时差

def test_compare_check_late(client):
    publish_pair(client, ref_pulse=500, chk_pulse=503, rnd=1)  # 待核站晚 30ms
    r = compare(client)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["decidable"] is True
    assert d["direction"] == "晚"
    assert d["offset_ms"] == -30
    assert d["shift_samples"] == -3
    assert d["correlation"] == pytest.approx(1.0)
    assert (d["ref_version"], d["check_version"]) == (1, 1)
    assert len(d["correlations"]) == 101  # ±500ms 按 10ms 步进
    assert d["reason"] is None


def test_compare_check_early(client):
    publish_pair(client, ref_pulse=500, chk_pulse=495, rnd=1)  # 待核站早 50ms
    d = compare(client).json()
    assert d["decidable"] is True
    assert d["direction"] == "早"
    assert d["offset_ms"] == 50
    assert d["shift_samples"] == 5


def test_compare_aligned(client):
    publish_pair(client, ref_pulse=500, chk_pulse=500, rnd=1)
    d = compare(client).json()
    assert d["decidable"] is True
    assert d["direction"] == "同步"
    assert d["offset_ms"] == 0


def test_compare_window_param_iso8601(client):
    publish_pair(client, ref_pulse=500, chk_pulse=503, rnd=1)
    r = client.get(f"/api/compare?ref={REF}&check={CHK}"
                   "&window_start_ms=2023-11-14T22:13:20.000Z")
    assert r.status_code == 200
    assert r.json()["decidable"] is True
    assert r.json()["offset_ms"] == -30


# ---------------------------------------------------------------- 参数校验

def test_compare_rejects_misaligned_window(client):
    r = compare(client, wstart=W0 + 5)
    assert r.status_code == 400
    assert "整 10 秒" in r.json()["detail"]


def test_compare_rejects_same_station(client):
    r = compare(client, ref=REF, chk=REF)
    assert r.status_code == 400


# ---------------------------------------------------------------- 无法判定情形

def test_compare_unpublished(client):
    # 两侧都未发布
    d = compare(client).json()
    assert d["decidable"] is False
    assert d["reason_code"] == "ref_unpublished"
    assert d["ref_version"] is None and d["check_version"] is None

    # 波形已上传但未发布,同样不可用
    upload(client, "\n".join([
        row(REF, W0, 1, 0, wave(500)),
        row(CHK, W0, 1, 0, wave(500)),
    ]))
    assert compare(client).json()["reason_code"] == "ref_unpublished"

    # 只发布参照站
    assert publish(client, REF).status_code == 200
    d = compare(client).json()
    assert d["reason_code"] == "check_unpublished"
    assert d["ref_version"] == 1 and d["check_version"] is None


def test_compare_stale_then_republish(client):
    """补传使待核站窗过期 -> 无法判定;重新发布后按新版本出结论。"""
    publish_pair(client, ref_pulse=500, chk_pulse=503, rnd=1)

    # 补传:待核站脉冲改到 510(晚 100ms),现役窗立即过期
    r = upload(client, row(CHK, W0, 2, 0, wave(510)))
    assert r.json()["stale_windows"] == [{"station": CHK, "window_start_ms": W0}]

    d = compare(client).json()
    assert d["decidable"] is False
    assert d["reason_code"] == "check_stale"
    assert d["check_version"] == 1  # 过期窗版本仍如实报告

    # 重新发布(换版)后,结论必须反映新版本波形
    assert publish(client, CHK).status_code == 200
    d = compare(client).json()
    assert d["decidable"] is True
    assert d["direction"] == "晚" and d["offset_ms"] == -100
    assert (d["ref_version"], d["check_version"]) == (1, 2)


def test_compare_ref_stale(client):
    publish_pair(client, ref_pulse=500, chk_pulse=503, rnd=1)
    upload(client, row(REF, W0, 2, 0, wave(501)))
    d = compare(client).json()
    assert d["decidable"] is False
    assert d["reason_code"] == "ref_stale"


def test_compare_constant_waveform(client):
    # 参照站常值
    upload(client, "\n".join([
        row(REF, W0, 1, 0, [7] * 1000),
        row(CHK, W0, 1, 0, wave(500)),
    ]))
    for st in (REF, CHK):
        assert publish(client, st).status_code == 200
    d = compare(client).json()
    assert d["decidable"] is False
    assert d["reason_code"] == "ref_constant"

    # 待核站常值(先把参照站换成非常值波形)
    upload(client, row(REF, W0, 2, 0, wave(500)))
    assert publish(client, REF).status_code == 200
    upload(client, row(CHK, W0, 2, 0, [3] * 1000))
    assert publish(client, CHK).status_code == 200
    d = compare(client).json()
    assert d["decidable"] is False
    assert d["reason_code"] == "check_constant"


def test_compare_peak_tie(client):
    """周期波形在多个偏移处相关同为 1.0 -> 峰值并列,无法判定。"""
    pattern = [0, 1, 0, -1] * 250  # 周期 4 样点,非常值
    upload(client, "\n".join([
        row(REF, W0, 1, 0, pattern),
        row(CHK, W0, 1, 0, pattern),
    ]))
    for st in (REF, CHK):
        assert publish(client, st).status_code == 200
    d = compare(client).json()
    assert d["decidable"] is False
    assert d["reason_code"] == "peak_tie"
    assert d["correlation"] == pytest.approx(1.0)
    assert len(d["tied_shifts_ms"]) > 1
    assert 0 in d["tied_shifts_ms"] and 40 in d["tied_shifts_ms"]


def test_compare_low_correlation(client):
    """两窗为互不相关的噪声 -> 最高相关系数低于 0.8,无法判定。"""
    r1, r2 = random.Random(11), random.Random(22)
    ref_w = [int(r1.random() * 201) - 100 for _ in range(1000)]
    chk_w = [int(r2.random() * 201) - 100 for _ in range(1000)]
    upload(client, "\n".join([
        row(REF, W0, 1, 0, ref_w),
        row(CHK, W0, 1, 0, chk_w),
    ]))
    for st in (REF, CHK):
        assert publish(client, st).status_code == 200
    d = compare(client).json()
    assert d["decidable"] is False
    assert d["reason_code"] == "low_correlation"
    assert d["correlation"] < 0.8
    assert d["best_shift_ms"] is not None


# ---------------------------------------------------------------- 换版与交错一致性

def test_compare_republish_updates_result(client):
    """换版(重新发布)后,比对结论必须来自新版本波形与版本号。"""
    publish_pair(client, ref_pulse=500, chk_pulse=503, rnd=1)
    d = compare(client).json()
    assert d["offset_ms"] == -30 and d["check_version"] == 1

    # 两站都换版:待核站脉冲改到 495(早 50ms)
    publish_pair(client, ref_pulse=500, chk_pulse=495, rnd=2)
    d = compare(client).json()
    assert d["decidable"] is True
    assert d["direction"] == "早" and d["offset_ms"] == 50
    assert (d["ref_version"], d["check_version"]) == (2, 2)


def test_compare_interleaved_backfill_republish(client):
    """补传/换版与比对交错:可判定结论必须来自同一已提交状态。

    不变式:版本 v 的窗中 REF 脉冲在 500+10v、CHK 脉冲在 505+10v,
    即可判定结论的 offset_ms 恒为 -50,且两窗版本必须相等
    (上传把两窗同时标过期,只有同版本双现役的状态才可判定)。
    """
    publish_pair(client, ref_pulse=510, chk_pulse=515, rnd=1)
    errors, results = [], []
    stop = threading.Event()

    def writer():
        try:
            for v in range(2, 15):
                text = "\n".join([
                    row(REF, W0, v, 0, wave(500 + 10 * v)),
                    row(CHK, W0, v, 0, wave(505 + 10 * v)),
                ])
                assert upload(client, text).status_code == 200
                for st in (REF, CHK):
                    assert publish(client, st).status_code == 200
        except Exception as e:  # noqa: BLE001
            errors.append(f"writer: {e}")
        finally:
            stop.set()

    def reader():
        try:
            while not stop.is_set():
                r = compare(client)
                assert r.status_code == 200
                d = r.json()
                results.append(d)
                if d["decidable"]:
                    # 两窗版本一致(同一已提交状态),时差恒为 -50ms
                    assert d["ref_version"] == d["check_version"], d
                    assert d["offset_ms"] == -50, d
                    assert d["correlation"] > 0.99, d
                else:
                    # 交错期间只允许"过期"类无法判定
                    assert d["reason_code"] in {"ref_stale", "check_stale"}, d
        except Exception as e:  # noqa: BLE001
            errors.append(f"reader: {e}")

    threads = [threading.Thread(target=writer)] + \
              [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert any(d["decidable"] for d in results)
    # 收尾:最终版本 14,结论与新版本一致
    d = compare(client).json()
    assert d["decidable"] is True
    assert d["ref_version"] == d["check_version"] == 14
    assert d["offset_ms"] == -50
