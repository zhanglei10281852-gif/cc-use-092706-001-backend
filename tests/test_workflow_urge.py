from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.clock import FrozenClock, to_storage
from app.core.errors import ConflictError, PermissionDeniedError, ValidationError
from app.core.security import Principal
from app.database import get_connection, transaction
from app.repositories.business import PetitionRepository
from app.services.workflow import PetitionWorkflowService


def _count_urges(petition_id: int) -> int:
    return int(get_connection().execute(
        "SELECT COUNT(*) FROM petition_urges WHERE petition_id=?", (petition_id,)
    ).fetchone()[0])


def _count_urge_flows(petition_id: int) -> int:
    return int(get_connection().execute(
        "SELECT COUNT(*) FROM petition_flow_records WHERE petition_id=? AND action='催办'", (petition_id,)
    ).fetchone()[0])


def _count_audit_events() -> int:
    return int(get_connection().execute("SELECT COUNT(*) FROM audit_events").fetchone()[0])


def make_active_petition(client, admin, *, name: str = "生境巡护一科") -> tuple[int, int, dict]:
    department = client.post(
        "/api/departments",
        headers=admin["headers"],
        json={"name": name, "manager": "值班主管", "phone": "13800000001"},
    )
    assert department.status_code == 201, department.text
    department_id = department.json()["id"]

    role = client.post(
        "/api/roles",
        headers=admin["headers"],
        json={"code": "habitat.handler", "name": "生境巡护员", "permission_codes": ["petitions.read", "petitions.write"]},
    )
    assert role.status_code == 201, role.text
    user = client.post(
        "/api/users",
        headers=admin["headers"],
        json={
            "username": "habitat.clerk",
            "password": "Clerk!23456",
            "display_name": "巡护员甲",
            "department_id": department_id,
            "role_codes": ["habitat.handler"],
        },
    )
    assert user.status_code == 201, user.text
    login = client.post(
        "/api/auth/login",
        json={"username": "habitat.clerk", "password": "Clerk!23456", "client_label": "tests"},
    )
    assert login.status_code == 200, login.text
    clerk = {"token": login.json()["token"], "headers": {"Authorization": f"Bearer {login.json()['token']}"}}

    created = client.post(
        "/petitions",
        json={"type": "投诉举报", "target": "湿地围栏维护", "content": "生境维护事项已逾期"},
    )
    assert created.status_code == 201, created.text
    petition_id = created.json()["id"]

    receipt = client.post(
        f"/api/petition-workflow/{petition_id}/transition",
        headers=admin["headers"],
        json={"target_status": "待分派"},
    )
    assert receipt.status_code == 200, receipt.text
    assigned = client.post(
        f"/api/petition-workflow/{petition_id}/transition",
        headers=admin["headers"],
        json={"target_status": "办理中", "department_id": department_id},
    )
    assert assigned.status_code == 200, assigned.text
    return petition_id, department_id, clerk


@pytest.fixture()
def admin_principal(admin) -> Principal:
    return Principal(
        user_id=1,
        username="admin",
        display_name="系统管理员",
        department_id=None,
        permissions=frozenset({"*"}),
        session_id=1,
    )


# ------------------------- 接口层 -------------------------

def test_api_rejects_whitespace_reason_without_side_effects(client, admin):
    petition_id, _, clerk = make_active_petition(client, admin)
    audit_before = _count_audit_events()

    response = client.post(
        f"/api/petition-workflow/{petition_id}/urge",
        headers=clerk["headers"],
        json={"reason": "     "},
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][-1] == "reason"
    # 空白原因在进入领域逻辑/事务前被拒绝，不留催办、流程或审计痕迹
    assert _count_urges(petition_id) == 0
    assert _count_urge_flows(petition_id) == 0
    assert _count_audit_events() == audit_before


def test_api_accepts_normal_reason_and_persists_trimmed(client, admin):
    petition_id, _, clerk = make_active_petition(client, admin)

    response = client.post(
        f"/api/petition-workflow/{petition_id}/urge",
        headers=clerk["headers"],
        json={"reason": "  请于两日内反馈围栏维护进展  "},
    )

    assert response.status_code == 201, response.text
    assert response.json()["reason"] == "请于两日内反馈围栏维护进展"
    assert response.json()["operator"] == "巡护员甲"

    detail = client.get(f"/petitions/{petition_id}").json()
    assert detail["urge_records"][0]["reason"] == "请于两日内反馈围栏维护进展"
    urge_flow = [record for record in detail["flow_records"] if record["action"] == "催办"]
    assert len(urge_flow) == 1
    assert urge_flow[0]["remark"] == "请于两日内反馈围栏维护进展"
    assert urge_flow[0]["operator"] == "巡护员甲"


# ------------------------- 服务层 -------------------------

def test_service_normalizes_reason_once_and_keeps_history_order(client, admin, admin_principal):
    petition_id, _, clerk = make_active_petition(client, admin)
    clock = FrozenClock(datetime(2026, 9, 30, 8, 0, tzinfo=UTC))

    with transaction(immediate=True) as connection:
        service = PetitionWorkflowService(connection, clock)
        result = service.urge(admin_principal, petition_id, "\t 请立即巡护并回填记录 \n")
        first_id = result["id"]
        clock.advance(minutes=5)
        second = service.urge(admin_principal, petition_id, "第二次催办")
        second_id = second["id"]

    assert result["reason"] == "请立即巡护并回填记录"
    assert second["reason"] == "第二次催办"
    assert second_id > first_id

    detail = PetitionRepository(get_connection()).detail(petition_id)
    assert detail is not None
    # 催办记录按新到旧，流程历史按发生顺序（旧到新）
    assert [row["reason"] for row in detail["urge_records"]] == ["第二次催办", "请立即巡护并回填记录"]
    urge_flow = [record for record in detail["flow_records"] if record["action"] == "催办"]
    assert [record["remark"] for record in urge_flow] == ["请立即巡护并回填记录", "第二次催办"]
    assert urge_flow[0]["created_at"] == to_storage(datetime(2026, 9, 30, 8, 0, tzinfo=UTC))
    assert urge_flow[1]["created_at"] == to_storage(datetime(2026, 9, 30, 8, 5, tzinfo=UTC))


def test_service_rejects_blank_reason_before_any_lookup(client, admin, admin_principal):
    petition_id, _, clerk = make_active_petition(client, admin)

    # 规范化先于信访件查询：即使件不存在，空白原因也按校验失败处理
    with transaction(immediate=True) as connection:
        with pytest.raises(ValidationError):
            PetitionWorkflowService(connection).urge(admin_principal, 999999, " \n\t ")

    with transaction(immediate=True) as connection:
        with pytest.raises(ValidationError):
            PetitionWorkflowService(connection).urge(admin_principal, petition_id, "   ")

    assert _count_urges(petition_id) == 0
    assert _count_urge_flows(petition_id) == 0


def test_service_still_enforces_permission_for_valid_reason(client, admin):
    petition_id, _, clerk = make_active_petition(client, admin)
    unauthorized = Principal(
        user_id=2,
        username="outsider",
        display_name="无关账号",
        department_id=None,
        permissions=frozenset(),
        session_id=2,
    )

    with transaction(immediate=True) as connection:
        with pytest.raises(PermissionDeniedError):
            PetitionWorkflowService(connection).urge(unauthorized, petition_id, "正常的催办原因")

    assert _count_urges(petition_id) == 0
    assert _count_urge_flows(petition_id) == 0


def test_service_still_rejects_urge_in_other_statuses(client, admin, admin_principal):
    # 新建信访件停留在“待签收”，不属于可催办状态
    created = client.post(
        "/petitions",
        json={"type": "意见建议", "target": "栖息地补水", "content": "建议增加补水频次"},
    )
    petition_id = created.json()["id"]

    with transaction(immediate=True) as connection:
        with pytest.raises(ConflictError):
            PetitionWorkflowService(connection).urge(admin_principal, petition_id, "请尽快处理")

    assert _count_urges(petition_id) == 0
    assert _count_urge_flows(petition_id) == 0


# ------------------------- 持久化边界 -------------------------

def test_legacy_blank_urge_records_remain_readable(client, admin):
    petition_id, _, clerk = make_active_petition(client, admin)
    connection = get_connection()
    connection.execute(
        "INSERT INTO petition_urges(petition_id,reason,operator,created_at) VALUES(?,?,?,?)",
        (petition_id, "   ", "历史值班主管", "2026-09-01T00:00:00+00:00"),
    )

    # 旧的空白催办记录不因新校验而无法读取
    detail = PetitionRepository(connection).detail(petition_id)
    assert detail is not None
    legacy = [row for row in detail["urge_records"] if row["operator"] == "历史值班主管"]
    assert len(legacy) == 1
    assert legacy[0]["reason"] == "   "

    response = client.get(f"/petitions/{petition_id}")
    assert response.status_code == 200
    assert any(row["reason"] == "   " for row in response.json()["urge_records"])


def test_other_transitions_still_work_after_urge_hardening(client, admin):
    petition_id, _, clerk = make_active_petition(client, admin)

    # 待审核状态仍允许催办
    submit = client.post(
        f"/api/petition-workflow/{petition_id}/transition",
        headers=clerk["headers"],
        json={"target_status": "待审核", "result": "围栏已安排维修"},
    )
    assert submit.status_code == 200, submit.text
    urge = client.post(
        f"/api/petition-workflow/{petition_id}/urge",
        headers=clerk["headers"],
        json={"reason": "请加快审核"},
    )
    assert urge.status_code == 201, urge.text

    approve = client.post(
        f"/api/petition-workflow/{petition_id}/transition",
        headers=clerk["headers"],
        json={"target_status": "已办结", "opinion": "同意办结"},
    )
    assert approve.status_code == 200, approve.text

    # 办结后状态流转约束依旧生效
    closed_urge = client.post(
        f"/api/petition-workflow/{petition_id}/urge",
        headers=clerk["headers"],
        json={"reason": "办结后不应再催办"},
    )
    assert closed_urge.status_code == 409

    detail = client.get(f"/petitions/{petition_id}").json()
    assert detail["status"] == "已办结"
    assert [record["reason"] for record in detail["urge_records"]] == ["请加快审核"]
