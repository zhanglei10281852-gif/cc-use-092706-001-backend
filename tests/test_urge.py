from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.clock import FrozenClock, to_storage
from app.core.errors import ConflictError, PermissionDeniedError, ValidationError
from app.core.security import Principal
from app.database import get_connection, transaction
from app.repositories.business import PetitionRepository
from app.services.auth import AuthService
from app.services.workflow import PetitionWorkflowService


def create_department(client) -> int:
    response = client.post(
        "/departments",
        json={"name": "生境维护组", "manager": "李主任", "phone": "010-12345678"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def create_processing_petition(client, department_id: int) -> int:
    """走既有旧版接口准备一条“办理中”的信访件，同时产生 3 条历史流水。"""
    response = client.post(
        "/petitions",
        json={"type": "意见建议", "target": "奥森湿地巡护", "content": "逾期生境维护事项需催办", "contact": "13800000000"},
    )
    assert response.status_code == 201, response.text
    petition_id = response.json()["id"]
    assert client.post(f"/petitions/{petition_id}/receive").status_code == 200
    assign = client.post(
        f"/petitions/{petition_id}/assign",
        json={"department_id": department_id, "deadline_days": 5},
    )
    assert assign.status_code == 200, assign.text
    return petition_id


def ledger_counts(petition_id: int) -> tuple[int, int, int]:
    conn = get_connection()
    urges = conn.execute("SELECT COUNT(*) FROM petition_urges WHERE petition_id=?", (petition_id,)).fetchone()[0]
    flows = conn.execute("SELECT COUNT(*) FROM petition_flow_records WHERE petition_id=?", (petition_id,)).fetchone()[0]
    audits = int(conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0])
    return urges, flows, audits


@pytest.fixture()
def processing_petition(client, admin) -> int:
    department_id = create_department(client)
    # bootstrap 管理员默认无部门，归入承办部门以满足数据范围校验
    user_id = client.get("/api/auth/me", headers=admin["headers"]).json()["user_id"]
    response = client.patch(f"/api/users/{user_id}", json={"department_id": department_id}, headers=admin["headers"])
    assert response.status_code == 200, response.text
    return create_processing_petition(client, department_id)


@pytest.fixture()
def admin_principal(admin, processing_petition) -> Principal:
    # processing_petition 已把管理员归入承办部门，principal 在其后解析以带上数据范围
    return AuthService(get_connection()).principal(admin["token"])


# ---------- 接口层 ----------

@pytest.mark.parametrize("reason", ["   ", "\t\n ", "　　"])
def test_api_rejects_blank_urge_reason_without_side_effects(client, admin, processing_petition, reason):
    petition_id = processing_petition
    before = ledger_counts(petition_id)

    response = client.post(
        f"/api/petition-workflow/{petition_id}/urge",
        json={"reason": reason},
        headers=admin["headers"],
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"

    assert ledger_counts(petition_id) == before
    status = get_connection().execute("SELECT status FROM petitions WHERE id=?", (petition_id,)).fetchone()[0]
    assert status == "办理中"


def test_api_rejects_missing_and_oversized_reason(client, admin, processing_petition):
    petition_id = processing_petition

    missing = client.post(f"/api/petition-workflow/{petition_id}/urge", json={}, headers=admin["headers"])
    assert missing.status_code == 422
    oversized = client.post(
        f"/api/petition-workflow/{petition_id}/urge",
        json={"reason": "x" * 1001},
        headers=admin["headers"],
    )
    assert oversized.status_code == 422
    assert ledger_counts(petition_id) == (0, 3, ledger_counts(petition_id)[2])


def test_api_normal_reason_is_trimmed_and_recorded_in_history_order(client, admin, processing_petition):
    petition_id = processing_petition

    response = client.post(
        f"/api/petition-workflow/{petition_id}/urge",
        json={"reason": "  请三日内反馈维护进展 "},
        headers=admin["headers"],
    )
    assert response.status_code == 201, response.text
    assert response.json()["reason"] == "请三日内反馈维护进展"

    detail = client.get(f"/petitions/{petition_id}").json()
    assert detail["urge_records"][0]["reason"] == "请三日内反馈维护进展"
    flow_actions = [item["action"] for item in detail["flow_records"]]
    assert flow_actions == ["提交信访件", "信访办签收", "分派任务，办结时限5天", "催办"]
    assert detail["flow_records"][-1]["remark"] == "请三日内反馈维护进展"


def test_api_urge_requires_authentication(client, processing_petition):
    response = client.post(f"/api/petition-workflow/{processing_petition}/urge", json={"reason": "正常原因"})
    assert response.status_code == 401


def test_api_legacy_router_also_rejects_blank_reason(client, admin, processing_petition):
    petition_id = processing_petition
    response = client.post(
        f"/petitions/{petition_id}/urge",
        json={"reason": "  ", "operator": "值班主管"},
    )
    assert response.status_code == 422, response.text
    assert get_connection().execute(
        "SELECT COUNT(*) FROM petition_urges WHERE petition_id=?", (petition_id,)
    ).fetchone()[0] == 0

    ok = client.post(
        f"/petitions/{petition_id}/urge",
        json={"reason": " 旧接口正常催办 ", "operator": "值班主管"},
    )
    assert ok.status_code == 200, ok.text
    detail = client.get(f"/petitions/{petition_id}").json()
    assert detail["urge_records"][0]["reason"] == "旧接口正常催办"


# ---------- 服务层 ----------

@pytest.mark.parametrize("reason", ["   ", "\t", " \n 　 "])
def test_service_blank_reason_rejected_before_any_persistence(admin, admin_principal, processing_petition, reason):
    petition_id = processing_petition
    before = ledger_counts(petition_id)
    clock = FrozenClock(datetime(2026, 9, 30, 8, 0, tzinfo=UTC))

    with pytest.raises(ValidationError):
        with transaction(immediate=True) as connection:
            PetitionWorkflowService(connection, clock).urge(admin_principal, petition_id, reason)

    assert ledger_counts(petition_id) == before


def test_service_normal_reason_uses_existing_permission_transaction_and_order(admin, admin_principal, processing_petition):
    petition_id = processing_petition
    clock = FrozenClock(datetime(2026, 9, 30, 8, 0, tzinfo=UTC))

    with transaction(immediate=True) as connection:
        saved = PetitionWorkflowService(connection, clock).urge(
            admin_principal, petition_id, "  请尽快完成生境维护 "
        )

    assert saved["reason"] == "请尽快完成生境维护"
    assert saved["operator"] == admin_principal.display_name
    assert saved["created_at"] == to_storage(clock.now())

    detail = PetitionRepository(get_connection()).detail(petition_id)
    assert detail["urge_records"][0]["reason"] == "请尽快完成生境维护"
    assert [row["action"] for row in detail["flow_records"]][-1] == "催办"
    assert detail["flow_records"][-1]["remark"] == "请尽快完成生境维护"
    assert detail["status"] == "办理中"


def test_service_blank_reason_rejected_before_status_guard(client, admin, admin_principal):
    # 空白校验先于状态守卫：待签收件收到空白催办原因也不产生任何写入
    petition_id = client.post(
        "/petitions",
        json={"type": "求助咨询", "target": "巡护道标识", "content": "待签收件"},
    ).json()["id"]
    before = ledger_counts(petition_id)

    with transaction(immediate=True) as connection:
        with pytest.raises(ValidationError):
            PetitionWorkflowService(connection).urge(admin_principal, petition_id, "   ")

    assert ledger_counts(petition_id) == before


def test_service_valid_reason_still_enforces_permissions(admin_principal, processing_petition):
    unauthorized = Principal(
        user_id=999,
        username="nobody",
        display_name="无权限人员",
        department_id=None,
        permissions=frozenset(),
        session_id=999,
    )
    with transaction(immediate=True) as connection:
        with pytest.raises(PermissionDeniedError):
            PetitionWorkflowService(connection).urge(unauthorized, processing_petition, "正常催办原因")
    assert ledger_counts(processing_petition)[:2] == (0, 3)


def test_service_other_transitions_remain_available(admin, admin_principal, processing_petition):
    petition_id = processing_petition

    # 催办后仍可提交审核、审核通过
    with transaction(immediate=True) as connection:
        urged = PetitionWorkflowService(connection).urge(admin_principal, petition_id, "第一次催办")
    assert urged["reason"] == "第一次催办"
    with transaction(immediate=True) as connection:
        reviewing = PetitionWorkflowService(connection).transition(
            admin_principal, petition_id, "待审核", result="维护已完成"
        )
    assert reviewing["status"] == "待审核"

    # 待审核期间仍可催办
    with transaction(immediate=True) as connection:
        saved = PetitionWorkflowService(connection).urge(admin_principal, petition_id, "待审核期间催办")
    assert saved["reason"] == "待审核期间催办"

    # 办结后催办仍被状态守卫拒绝，且不产生台账/流水
    with transaction(immediate=True) as connection:
        PetitionWorkflowService(connection).transition(
            admin_principal, petition_id, "已办结", opinion="同意办结"
        )
    before = ledger_counts(petition_id)
    with transaction(immediate=True) as connection:
        with pytest.raises(ConflictError):
            PetitionWorkflowService(connection).urge(admin_principal, petition_id, "已办结不能催办")
    assert ledger_counts(petition_id) == before


# ---------- 持久化边界：旧记录读取 ----------

def test_legacy_blank_urge_records_still_readable_and_new_records_keep_order(
    client, admin, admin_principal, processing_petition
):
    petition_id = processing_petition
    conn = get_connection()
    legacy_ts = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO petition_urges(petition_id,reason,operator,created_at) VALUES(?,?,?,?)",
        (petition_id, "   ", "旧系统值班主管", legacy_ts),
    )
    conn.commit()

    # 旧的空白记录不被新校验影响：仓库层与旧版详情接口都能读出
    detail = PetitionRepository(conn).detail(petition_id)
    assert [row["reason"] for row in detail["urge_records"]] == ["   "]
    api_detail = client.get(f"/petitions/{petition_id}").json()
    assert api_detail["urge_records"][0]["operator"] == "旧系统值班主管"

    # 新催办按 id 倒序追加，排在旧记录之前，历史顺序不被打乱
    with transaction(immediate=True) as connection:
        saved = PetitionWorkflowService(connection).urge(admin_principal, petition_id, "新催办")
    detail = PetitionRepository(get_connection()).detail(petition_id)
    ids = [row["id"] for row in detail["urge_records"]]
    assert ids == sorted(ids, reverse=True)
    assert detail["urge_records"][0]["id"] == saved["id"]
    assert detail["urge_records"][1]["reason"] == "   "
