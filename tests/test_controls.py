"""容量管制时段（临时限流）集成测试。

覆盖：
* 登记左闭右开管制时段后，编排按通行期间每一分钟的有效上限核算
  （时段内用管制上限、时段外仍用原容量），封闭规则照常生效；
* 通道不存在 / 起止无效 / 起点早于当前分钟 / 时段重叠 -> 整次拒绝，
  不改状态、不产生修订；
* 新上限使已出发组剩余路线在任一分钟超载 -> 409，返回冲突分钟、
  占用量与相关组，整次拒绝且不改状态；
* 成功后仅重排未出发组、保留已出发组路线，并产生一条含操作参数与
  编排差异的 controls 修订；
* 场景重置清除管制，旧修订仍可查询；
* 占用查询显示各分钟有效上限，既有查询字段保持兼容。
"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

# 与 tests/test_api.py 同进程运行时 app.main 只会加载一次（以先加载者
# 的环境变量为准）；此处设置保证本文件可独立运行。断言均不依赖修订号
# 的绝对取值，共享数据库时同样成立。
_tmpdir = tempfile.mkdtemp(prefix="evac-test-controls-")
os.environ["EVAC_DB_PATH"] = os.path.join(_tmpdir, "test.db")
os.environ["EVAC_SEED"] = "false"

from app.main import app  # noqa: E402


# 1 --P1(4/30)--> 2 --P2(1/30)--> 5(出口)
LINE_SCENARIO = {
    "passages": [
        {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 30},
        {"id": 2, "src": 2, "dst": 5, "minutes": 1, "capacity": 30},
    ],
    "exits": [5],
    "closures": [],
    "groups": [
        {"id": 1, "size": 25, "origin": 1, "earliest_departure": 0,
         "priority": 10, "deadline": 30},
    ],
}

# 与 tests/test_api.py 相同的场景（含封闭 [3,10) 的通道 1）
FULL_SCENARIO = {
    "passages": [
        {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 30},
        {"id": 2, "src": 2, "dst": 3, "minutes": 3, "capacity": 30},
        {"id": 3, "src": 3, "dst": 5, "minutes": 2, "capacity": 30},
        {"id": 5, "src": 2, "dst": 6, "minutes": 2, "capacity": 40},
        {"id": 7, "src": 7, "dst": 5, "minutes": 5, "capacity": 20},
    ],
    "exits": [5, 6],
    "closures": [{"edge_id": 1, "start_minute": 3, "end_minute": 10}],
    "groups": [
        {"id": 1, "size": 30, "origin": 1, "earliest_departure": 0,
         "priority": 10, "deadline": 30},
        {"id": 3, "size": 10, "origin": 2, "earliest_departure": 0,
         "priority": 20, "deadline": 20},
        {"id": 4, "size": 15, "origin": 7, "earliest_departure": 0,
         "priority": 5, "deadline": 15},
    ],
}


@pytest.fixture()
def client():
    with TestClient(app) as c:
        resp = c.post("/scenario", json=LINE_SCENARIO)
        assert resp.status_code == 200
        yield c


@pytest.fixture()
def full_client():
    with TestClient(app) as c:
        resp = c.post("/scenario", json=FULL_SCENARIO)
        assert resp.status_code == 200
        yield c


def by_id(plan):
    return {g["group_id"]: g for g in plan["groups"]}


def revs(c):
    return c.get("/revisions").json()["revisions"]


# --------------------------------------------------------------------------
# 编排按每分钟有效上限核算
# --------------------------------------------------------------------------

def test_control_within_window_capacity_forces_wait(client):
    # 组 1（25 人）原计划 t=0 进入通道 1；管制 [1,6) 上限 24 < 25，
    # 通行区间 [t,t+4) 不能覆盖任何管制分钟 -> 最早第 6 分钟进入
    resp = client.post("/controls", json={
        "edge_id": 1, "start_minute": 1, "end_minute": 6, "capacity": 24})
    assert resp.status_code == 201
    g1 = by_id(resp.json())[1]
    assert g1["status"] == "scheduled"
    assert g1["departure_minute"] == 6
    assert g1["arrival_minute"] == 11
    assert g1["route"] == [1, 2]


def test_control_capacity_exactly_fits_passes(client):
    # 管制上限 25 恰好容纳 25 人：逐分钟核算通过，仍第 0 分钟出发
    resp = client.post("/controls", json={
        "edge_id": 1, "start_minute": 1, "end_minute": 6, "capacity": 25})
    assert resp.status_code == 201
    g1 = by_id(resp.json())[1]
    assert g1["departure_minute"] == 0
    assert g1["arrival_minute"] == 5


def test_control_outside_window_uses_original_capacity(client):
    # 管制 [1,6) 上限 20：25 人的组 1 在分钟 1..3 超限，须等到第 6 分钟
    # （[6,10) 全部在时段外，按原容量 30 核算）；5 人的组 2 逐分钟核算
    # 通过（分钟 0 时段外 5<=30，分钟 1..3 时段内 5<=20），仍 t=0 出发。
    client.post("/groups", json=[
        {"id": 2, "size": 5, "origin": 1, "earliest_departure": 0,
         "priority": 5, "deadline": 30}])
    resp = client.post("/controls", json={
        "edge_id": 1, "start_minute": 1, "end_minute": 6, "capacity": 20})
    assert resp.status_code == 201
    groups = by_id(resp.json())
    assert groups[1]["departure_minute"] == 6
    assert groups[1]["arrival_minute"] == 11
    assert groups[2]["departure_minute"] == 0
    assert groups[2]["arrival_minute"] == 5


def test_control_window_raising_capacity_lets_groups_share(client):
    # 管制上限 40 高于原容量 30：时段内两组（25+5=30<=40）可同时通行
    client.post("/groups", json=[
        {"id": 2, "size": 5, "origin": 1, "earliest_departure": 0,
         "priority": 5, "deadline": 30}])
    resp = client.post("/controls", json={
        "edge_id": 1, "start_minute": 0, "end_minute": 10, "capacity": 40})
    assert resp.status_code == 201
    groups = by_id(resp.json())
    assert groups[1]["departure_minute"] == 0
    assert groups[2]["departure_minute"] == 0


def test_control_combined_with_closure(client):
    # 封闭规则照常生效：通道 1 封闭 [3,10) + 管制 [12,20) 上限 10，
    # 25 人组须同时避开两者 -> 最早第 20 分钟进入
    client.post("/closures", json={
        "edge_id": 1, "start_minute": 3, "end_minute": 10})
    resp = client.post("/controls", json={
        "edge_id": 1, "start_minute": 12, "end_minute": 20, "capacity": 10})
    assert resp.status_code == 201
    g1 = by_id(resp.json())[1]
    assert g1["departure_minute"] == 20
    assert g1["arrival_minute"] == 25


# --------------------------------------------------------------------------
# 参数校验与整次拒绝
# --------------------------------------------------------------------------

def test_control_invalid_params_rejected(client):
    n0 = len(revs(client))
    # 通道不存在
    assert client.post("/controls", json={
        "edge_id": 999, "start_minute": 0, "end_minute": 5,
        "capacity": 10}).status_code == 422
    # 起点不早于终点（等于/大于均非法）
    assert client.post("/controls", json={
        "edge_id": 1, "start_minute": 5, "end_minute": 5,
        "capacity": 10}).status_code == 422
    assert client.post("/controls", json={
        "edge_id": 1, "start_minute": 8, "end_minute": 5,
        "capacity": 10}).status_code == 422
    # 上限必须为正整数
    assert client.post("/controls", json={
        "edge_id": 1, "start_minute": 0, "end_minute": 5,
        "capacity": 0}).status_code == 422
    assert client.post("/controls", json={
        "edge_id": 1, "start_minute": 0, "end_minute": 5,
        "capacity": -3}).status_code == 422
    # 全部被拒：无管制记录、编排不变、无新修订
    assert client.get("/controls").json() == {"controls": []}
    assert by_id(client.get("/plan").json())[1]["departure_minute"] == 0
    assert len(revs(client)) == n0


def test_control_start_before_current_minute_rejected(client):
    assert client.post("/tick", json={"minutes": 2}).status_code == 200
    n0 = len(revs(client))
    resp = client.post("/controls", json={
        "edge_id": 1, "start_minute": 1, "end_minute": 5, "capacity": 10})
    assert resp.status_code == 422
    assert client.get("/controls").json() == {"controls": []}
    assert len(revs(client)) == n0
    # 起点等于当前分钟则允许（上限 30 不压缩已出发组占用，可登记）
    assert client.post("/controls", json={
        "edge_id": 1, "start_minute": 2, "end_minute": 5,
        "capacity": 30}).status_code == 201


def test_control_overlap_rejected(client):
    assert client.post("/controls", json={
        "edge_id": 1, "start_minute": 2, "end_minute": 6,
        "capacity": 10}).status_code == 201
    n0 = len(revs(client))
    # 与既有时段 [2,6) 相交的登记均整次拒绝（409）
    for a, b in ((0, 3), (3, 5), (5, 9), (0, 9), (2, 6)):
        resp = client.post("/controls", json={
            "edge_id": 1, "start_minute": a, "end_minute": b,
            "capacity": 8})
        assert resp.status_code == 409, (a, b)
        assert resp.json()["error"] == "CONTROL_WINDOW_OVERLAP"
    # 首尾相接不算重叠；其他通道不受限制
    assert client.post("/controls", json={
        "edge_id": 1, "start_minute": 6, "end_minute": 9,
        "capacity": 8}).status_code == 201
    assert client.post("/controls", json={
        "edge_id": 1, "start_minute": 0, "end_minute": 2,
        "capacity": 8}).status_code == 201
    assert client.post("/controls", json={
        "edge_id": 2, "start_minute": 2, "end_minute": 6,
        "capacity": 8}).status_code == 201
    # 三次成功 + 既有一次，共四条修订；五次重叠拒绝不产生修订
    assert len(revs(client)) == n0 + 3
    listed = client.get("/controls").json()["controls"]
    assert [(c["edge_id"], c["start_minute"], c["end_minute"])
            for c in listed] == [(1, 0, 2), (1, 2, 6), (1, 6, 9), (2, 2, 6)]


# --------------------------------------------------------------------------
# 已出发组超载 -> 409 整次拒绝
# --------------------------------------------------------------------------

def test_control_overloading_departed_group_rejected(full_client):
    c = full_client
    # 组 3（10 人）t=0 进入通道 5，占用 [0,2)；推进 1 分钟后固定
    assert c.post("/tick", json={"minutes": 1}).status_code == 200
    assert by_id(c.get("/plan").json())[3]["status"] == "fixed"
    plan_before = c.get("/plan").json()
    n0 = len(revs(c))

    resp = c.post("/controls", json={
        "edge_id": 5, "start_minute": 1, "end_minute": 3, "capacity": 5})
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "CONTROL_OVERLOADS_DEPARTED_GROUP"
    # 冲突分钟、占用量与相关组
    assert body["conflicts"] == [{
        "edge_id": 5, "minute": 1, "used": 10, "capacity": 5,
        "groups": [{"group_id": 3, "size": 10}],
    }]
    # 整次拒绝且不改状态：无管制记录、编排不变、无新修订
    assert c.get("/controls").json() == {"controls": []}
    assert c.get("/plan").json() == plan_before
    assert len(revs(c)) == n0

    # 上限恰好等于占用（10<=10）则不超载，登记成功
    resp = c.post("/controls", json={
        "edge_id": 5, "start_minute": 1, "end_minute": 3, "capacity": 10})
    assert resp.status_code == 201


def test_control_overload_aggregates_multiple_departed_groups(client):
    # 两组均已出发且同分钟在途：超载核查按分钟汇总占用
    client.post("/groups", json=[
        {"id": 2, "size": 5, "origin": 1, "earliest_departure": 0,
         "priority": 5, "deadline": 30}])
    # 25+5=30<=30，两组均 t=0 进入通道 1；推进后均固定，剩余 [1,4)
    assert by_id(client.get("/plan").json())[2]["departure_minute"] == 0
    client.post("/tick", json={"minutes": 1})
    resp = client.post("/controls", json={
        "edge_id": 1, "start_minute": 1, "end_minute": 4, "capacity": 29})
    assert resp.status_code == 409
    conflicts = resp.json()["conflicts"]
    assert [cf["minute"] for cf in conflicts] == [1, 2, 3]
    for cf in conflicts:
        assert cf["used"] == 30
        assert cf["capacity"] == 29
        assert cf["groups"] == [{"group_id": 1, "size": 25},
                                {"group_id": 2, "size": 5}]


# --------------------------------------------------------------------------
# 成功登记：仅重排未出发组 + 修订
# --------------------------------------------------------------------------

def test_control_success_replans_only_undeparted(full_client):
    c = full_client
    # 推进 1 分钟：组 3、组 4（均 t=0 出发）固定，仅组 1 未出发
    assert c.post("/tick", json={"minutes": 1}).status_code == 200
    fixed3_before = by_id(c.get("/plan").json())[3]
    fixed4_before = by_id(c.get("/plan").json())[4]

    resp = c.post("/controls", json={
        "edge_id": 1, "start_minute": 5, "end_minute": 12, "capacity": 10})
    assert resp.status_code == 201
    groups = by_id(resp.json())
    # 组 1（30 人）：封闭 [3,10) 与管制 [5,12)/上限10 共同作用，
    # 最早第 12 分钟进入通道 1，第 18 分钟到达出口 6
    assert groups[1]["status"] == "scheduled"
    assert groups[1]["departure_minute"] == 12
    assert groups[1]["arrival_minute"] == 18
    assert groups[1]["route"] == [1, 5]
    # 已出发组路线与占用保持不变
    assert groups[3] == fixed3_before
    assert groups[4] == fixed4_before

    # 修订：operation=controls，detail 含操作参数，差异仅涉及未出发组
    detail = c.get(f"/revisions/{revs(c)[-1]['rev']}").json()
    assert detail["operation"] == "controls"
    assert detail["minute"] == 1
    assert detail["detail"] == {"edge_id": 1, "start_minute": 5,
                                "end_minute": 12, "capacity": 10}
    changes = {ch["group_id"]: ch for ch in detail["changes"]}
    assert set(changes) == {1}
    ch1 = changes[1]
    assert ch1["change"] == "changed"
    assert "departure_minute" in ch1["changed_fields"]
    assert ch1["before"]["departure_minute"] == 10
    assert ch1["after"]["departure_minute"] == 12
    # 快照与变更响应一致
    assert detail["snapshot"] == resp.json()


def test_control_revision_listed_and_survives_reset(client):
    assert client.post("/controls", json={
        "edge_id": 1, "start_minute": 2, "end_minute": 6,
        "capacity": 10}).status_code == 201
    entries = revs(client)
    rev = entries[-1]["rev"]
    assert entries[-1]["operation"] == "controls"
    detail_before = client.get(f"/revisions/{rev}").json()

    # 场景重置清除管制，但旧修订仍可查询且内容不变
    assert client.get("/controls").json()["controls"] != []
    assert client.post("/scenario", json=LINE_SCENARIO).status_code == 200
    assert client.get("/controls").json() == {"controls": []}
    assert client.get(f"/revisions/{rev}").json() == detail_before
    # 重置后管制不再影响编排：组 1 恢复第 0 分钟出发
    assert by_id(client.get("/plan").json())[1]["departure_minute"] == 0


# --------------------------------------------------------------------------
# 占用查询显示各分钟有效上限（既有字段保持兼容）
# --------------------------------------------------------------------------

def test_load_shows_effective_capacity_per_minute(client):
    # 组 1（25 人）在管制上限 25 下仍 t=0 进入通道 1，占用 [0,4)
    assert client.post("/controls", json={
        "edge_id": 1, "start_minute": 1, "end_minute": 3,
        "capacity": 25}).status_code == 201
    load = client.get("/passages/1/load").json()
    # 既有字段保持兼容
    assert load["edge_id"] == 1
    assert load["capacity"] == 30
    assert load["traversal_minutes"] == 4
    # 登记的管制时段随占用结果返回
    assert load["controls"] == [
        {"start_minute": 1, "end_minute": 3, "capacity": 25}]
    slots = {m["minute"]: m for m in load["minutes"]}
    assert set(slots) == {0, 1, 2, 3}
    # 时段内分钟显示管制上限，时段外显示原容量
    assert slots[0]["used"] == 25 and slots[0]["effective_capacity"] == 30
    assert slots[1]["used"] == 25 and slots[1]["effective_capacity"] == 25
    assert slots[2]["used"] == 25 and slots[2]["effective_capacity"] == 25
    assert slots[3]["used"] == 25 and slots[3]["effective_capacity"] == 30
    # ?minute=n 过滤同样带有效上限
    one = client.get("/passages/1/load?minute=2").json()
    assert [m["minute"] for m in one["minutes"]] == [2]
    assert one["minutes"][0]["effective_capacity"] == 25


def test_load_without_controls_keeps_compatible_shape(client):
    load = client.get("/passages/1/load").json()
    assert load["controls"] == []
    slot = load["minutes"][0]
    assert slot["effective_capacity"] == load["capacity"] == 30
    assert {"minute", "used", "groups", "effective_capacity"} <= set(slot)
