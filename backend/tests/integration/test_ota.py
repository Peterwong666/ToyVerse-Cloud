"""P9 OTA：**仅平台端可见**，且**推送能力由云服务商数据驱动**。

两条验收红线
------------
1. **Wi-Fi 方案必须被拒绝**（`cloud_providers.ota_support = UNSUPPORTED`）——
   京东 JoyInside 与火山方案的固件只能端侧升级，平台无法推送。
   这条**不是写死的 if**：判定读的是云服务商记录上的 `ota_support` 字段，
   因此新接一家支持 OTA 的 Wi-Fi 厂商时行为会自动跟着变。
2. **商户端拿不到 OTA**：固件是平台侧资产（一台设备的固件版本影响所有租户），
   商户既不该看到别的租户的设备在升什么版本，也不该有能力触发全网推送。
   `PlatformPerm.OTA_READ/WRITE` 是平台独占权限，商户令牌一律 403。

为什么「无文件的固件包」也要测
------------------------------
平台允许只登记元数据（内测包、待上传的真实包）。这类包**不能真正推送**，
必须逐台写 `FAILED` 并说明原因——而不是「假装推成功」。
这是 ADR-07「绝不伪造成功」在 OTA 上的落点，也是本文件里最容易写漏的一条。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.device import Device
from app.models.ops import OtaRecord
from tests.conftest import API_PREFIX

pytestmark = pytest.mark.integration

PLATFORM = f"{API_PREFIX}/platform"

#: 种子里两个演示产品：4G（集贤，支持 OTA）与 Wi-Fi（不支持）
PRODUCT_4G = "prod-t001-4g"
PRODUCT_WIFI = "prod-t001-cube"
TEMPLATE_4G = "tpl-4g-storyteller"
TEMPLATE_WIFI = "tpl-esp32s3-toy"


@pytest.fixture
async def demo(db: AsyncSession) -> AsyncSession:
    """灌入完整演示数据（含 4G/Wi-Fi 设备与一个无文件的演示固件包）。"""
    from app.db import seed as seed_module

    await seed_module._seed_catalog(db)
    await seed_module._seed_orders_devices(db)
    await seed_module._seed_miniapp(db)
    await seed_module._seed_ops(db)
    await db.commit()
    return db


@pytest.fixture
async def platform(client: httpx.AsyncClient, demo: Any) -> dict[str, str]:
    """平台超管请求头。"""
    resp = await client.post(
        f"{API_PREFIX}/auth/login",
        json={
            "account": settings.PLATFORM_ADMIN_ACCOUNT,
            "password": settings.PLATFORM_ADMIN_PASSWORD,
        },
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['accessToken']}"}


@pytest.fixture
async def merchant(client: httpx.AsyncClient, demo: Any) -> dict[str, str]:
    """t-001 商户请求头（用于断言 OTA 对商户不可见）。"""
    resp = await client.post(
        f"{API_PREFIX}/auth/login",
        json={
            "account": settings.MERCHANT_ADMIN_ACCOUNT,
            "password": settings.MERCHANT_ADMIN_PASSWORD,
        },
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['accessToken']}"}


async def _upload_package(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    *,
    template_id: str,
    version: str,
    with_file: bool = True,
) -> Any:
    """登记一个固件包（可选择带不带文件）。"""
    data: dict[str, Any] = {"templateId": template_id, "version": version, "releaseNotes": "测试"}
    files = (
        {"file": (f"fw-{version}.bin", b"\x00\x01\x02firmware-bytes", "application/octet-stream")}
        if with_file
        else None
    )
    resp = await client.post(f"{PLATFORM}/ota/packages", headers=headers, data=data, files=files)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# 一、仅平台端可见
# ---------------------------------------------------------------------------


class TestOtaIsPlatformOnly:
    """★ 固件是平台侧资产，商户端不得读写、更不得触发推送。"""

    @pytest.mark.parametrize(
        ("case", "method", "path", "body"),
        [
            ("包列表", "GET", f"{PLATFORM}/ota/packages", None),
            ("推送记录", "GET", f"{PLATFORM}/ota/records", None),
            (
                "登记固件包",
                "POST",
                f"{PLATFORM}/ota/packages",
                None,  # multipart 场景由下面的专门用例覆盖
            ),
        ],
        ids=["包列表", "推送记录", "登记固件包"],
    )
    async def test_merchant_is_rejected(
        self,
        client: httpx.AsyncClient,
        merchant: dict[str, str],
        case: str,
        method: str,
        path: str,
        body: dict[str, Any] | None,
    ) -> None:
        """商户 token 访问 OTA 端点 → 403 `PERMISSION_DENIED`。

        这里刻意断言 **403 而不是 404**：OTA 是平台独占能力，不是「不存在」，
        商户看到「无权限」才是正确的语义（与租户级资源的 404 口径不同——
        那里要避免探测存在性，这里不存在可被探测的他人资源）。
        """
        response = await client.request(method, path, headers=merchant, json=body)
        assert response.status_code == 403, f"[{case}] {response.status_code} {response.text[:200]}"
        assert response.json()["code"] == "PERMISSION_DENIED"

    async def test_merchant_cannot_push(
        self, client: httpx.AsyncClient, platform: dict[str, str], merchant: dict[str, str]
    ) -> None:
        """★ 商户 token 触发推送 → 403（即使他知道包 ID）。"""
        package = await _upload_package(
            client, platform, template_id=TEMPLATE_4G, version="1.7.0", with_file=True
        )
        response = await client.post(
            f"{PLATFORM}/ota/packages/{package['id']}/push",
            headers=merchant,
            json={"clientProductId": PRODUCT_4G},
        )
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "PERMISSION_DENIED"


# ---------------------------------------------------------------------------
# 二、Wi-Fi 方案必须被拒绝（数据驱动）
# ---------------------------------------------------------------------------


class TestWifiIsRejected:
    """★ 验收红线：Wi-Fi 产品的推送必须被拒绝，且给出方案级原因。"""

    async def test_push_to_wifi_product_is_rejected(
        self, client: httpx.AsyncClient, platform: dict[str, str]
    ) -> None:
        """★ 推 Wi-Fi 产品 → 409 `OTA_NOT_SUPPORTED`，`details` 说明是哪家云。

        判定来自 `cloud_providers.ota_support`，不是写死的厂商名：
        `details` 里必须带上 `otaSupport` / `cloudVendor` / `cloudProviderName`，
        运维才知道「是谁不支持」而不是只看一句「不支持」。
        """
        package = await _upload_package(
            client, platform, template_id=TEMPLATE_WIFI, version="2.0.0", with_file=True
        )
        response = await client.post(
            f"{PLATFORM}/ota/packages/{package['id']}/push",
            headers=platform,
            json={"clientProductId": PRODUCT_WIFI},
        )
        assert response.status_code == 409, f"{response.status_code} {response.text[:300]}"
        assert response.json()["code"] == "OTA_NOT_SUPPORTED"

        details = response.json()["details"]
        assert details["otaSupport"] == "UNSUPPORTED"
        assert details.get("cloudVendor"), "必须说明是哪家云服务商不支持"
        assert details.get("cloudProviderName")

    async def test_no_record_is_written_when_rejected(
        self, client: httpx.AsyncClient, platform: dict[str, str], db: AsyncSession
    ) -> None:
        """★ 被拒绝的推送**不得留下任何推送记录**。

        「拒绝」必须是真的没发生：若实现先建记录再判定支持性，会留下一批
        `PENDING` 僵尸记录，运营看板上表现为「一直在推送中」。
        """
        before = int(
            (await db.execute(select(func.count()).select_from(OtaRecord))).scalar_one()
        )
        package = await _upload_package(
            client, platform, template_id=TEMPLATE_WIFI, version="2.0.1", with_file=True
        )
        response = await client.post(
            f"{PLATFORM}/ota/packages/{package['id']}/push",
            headers=platform,
            json={"clientProductId": PRODUCT_WIFI},
        )
        assert response.status_code == 409
        await db.commit()  # 读一次最新状态，避免会话内的快照偏差
        after = int((await db.execute(select(func.count()).select_from(OtaRecord))).scalar_one())
        assert after == before, "被拒绝的推送不应写入任何 ota_records 行"


# ---------------------------------------------------------------------------
# 三、4G 方案：真实推送与逐台留痕
# ---------------------------------------------------------------------------


class TestFourGPush:
    """4G（集贤，`ota_support=SUPPORTED`）可以推送，且逐台留痕。"""

    async def test_push_succeeds_and_updates_device_version(
        self, client: httpx.AsyncClient, platform: dict[str, str], db: AsyncSession
    ) -> None:
        """★ 推送成功 → 记录 `SUCCESS`，且**设备的固件版本被更新**。

        「版本更新」是推送唯一的可验证结果。只写记录不改设备，运营会看到
        「已推送成功」但下次推送又推同一台——这类不一致必须挡住。
        """
        package = await _upload_package(
            client, platform, template_id=TEMPLATE_4G, version="1.9.0", with_file=True
        )
        response = await client.post(
            f"{PLATFORM}/ota/packages/{package['id']}/push",
            headers=platform,
            json={"clientProductId": PRODUCT_4G},
        )
        assert response.status_code == 200, f"{response.status_code} {response.text[:300]}"
        body = response.json()
        assert body["otaSupport"] == "SUPPORTED"
        assert body["requested"] >= 1, "该产品下应当有演示设备"
        assert body["failed"] == 0, f"不应有失败：{body['records']}"
        assert body["pushed"] == body["requested"]

        for record in body["records"]:
            assert record["status"] == "SUCCESS"
            assert record["toVersion"] == "1.9.0"
            device = await db.get(Device, record["deviceId"])
            assert device is not None
            assert device.firmware_version == "1.9.0", "推送成功必须更新设备固件版本"

        # 逐台记录了（而不是只留一条批次记录）
        rows = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(OtaRecord)
                    .where(OtaRecord.ota_package_id == package["id"])
                )
            ).scalar_one()
        )
        assert rows == body["requested"]

    async def test_repush_is_skipped(
        self, client: httpx.AsyncClient, platform: dict[str, str]
    ) -> None:
        """★ 已是最新版本的设备再推 → 计入 `skipped`（幂等，不重复写记录）。"""
        package = await _upload_package(
            client, platform, template_id=TEMPLATE_4G, version="1.9.1", with_file=True
        )
        first = await client.post(
            f"{PLATFORM}/ota/packages/{package['id']}/push",
            headers=platform,
            json={"clientProductId": PRODUCT_4G},
        )
        assert first.status_code == 200
        assert first.json()["pushed"] >= 1

        second = await client.post(
            f"{PLATFORM}/ota/packages/{package['id']}/push",
            headers=platform,
            json={"clientProductId": PRODUCT_4G},
        )
        assert second.status_code == 200, second.text
        body = second.json()
        assert body["pushed"] == 0, "重复推送不应再次推送"
        assert body["skipped"] == body["requested"], body

    async def test_package_without_file_fails_honestly(
        self, client: httpx.AsyncClient, platform: dict[str, str]
    ) -> None:
        """★ 无文件的固件包推送 → 逐台 `FAILED` 且原因可读（**绝不伪造成功**）。

        种子里那个 `otap-4g-130` 就是「只登记元数据」的演示包。
        """
        pytest.importorskip("sqlalchemy")
        listing = await client.get(
            f"{PLATFORM}/ota/packages", headers=platform, params={"templateId": TEMPLATE_4G}
        )
        assert listing.status_code == 200, listing.text
        without_file = [row for row in listing.json()["records"] if not row["hasFile"]]
        assert without_file, "种子应当提供一个无文件的演示固件包"
        package = without_file[0]

        response = await client.post(
            f"{PLATFORM}/ota/packages/{package['id']}/push",
            headers=platform,
            json={"clientProductId": PRODUCT_4G},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["pushed"] == 0
        assert body["failed"] >= 1
        for record in body["records"]:
            if record["status"] == "FAILED":
                assert record["errorMessage"], "失败必须给出原因，不能是空字符串"

    async def test_foreign_device_in_list_fails_that_row_only(
        self, client: httpx.AsyncClient, platform: dict[str, str], db: AsyncSession
    ) -> None:
        """★ 推送名单里混入**不属于该产品**的设备 → 只该行失败，不整批拒绝。"""
        wifi_device = (
            await db.execute(select(Device).where(Device.client_product_id == PRODUCT_WIFI))
        ).scalars().first()
        assert wifi_device is not None

        package = await _upload_package(
            client, platform, template_id=TEMPLATE_4G, version="1.9.2", with_file=True
        )
        response = await client.post(
            f"{PLATFORM}/ota/packages/{package['id']}/push",
            headers=platform,
            json={"clientProductId": PRODUCT_4G, "deviceIds": [wifi_device.id]},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["failed"] == 1, body
        assert body["pushed"] == 0
        assert body["records"][0]["errorMessage"]


# ---------------------------------------------------------------------------
# 四、包管理与记录查询
# ---------------------------------------------------------------------------


class TestPackageManagement:
    """固件包的登记 / 重复校验 / 删除级联。"""

    async def test_duplicate_version_is_rejected(
        self, client: httpx.AsyncClient, platform: dict[str, str]
    ) -> None:
        """同一模板下版本号唯一：重复登记 → 409（不静默覆盖）。"""
        await _upload_package(client, platform, template_id=TEMPLATE_4G, version="3.0.0")
        response = await client.post(
            f"{PLATFORM}/ota/packages",
            headers=platform,
            data={"templateId": TEMPLATE_4G, "version": "3.0.0"},
        )
        assert response.status_code == 409, f"{response.status_code} {response.text[:200]}"

    async def test_disallowed_extension_is_rejected(
        self, client: httpx.AsyncClient, platform: dict[str, str]
    ) -> None:
        """扩展名白名单：塞一个 .exe → 400（不落盘）。"""
        response = await client.post(
            f"{PLATFORM}/ota/packages",
            headers=platform,
            data={"templateId": TEMPLATE_4G, "version": "3.1.0"},
            files={"file": ("malware.exe", b"MZ", "application/octet-stream")},
        )
        assert response.status_code == 400, f"{response.status_code} {response.text[:200]}"

    async def test_delete_cascades_records(
        self, client: httpx.AsyncClient, platform: dict[str, str], db: AsyncSession
    ) -> None:
        """★ 删除固件包 → 级联删除其推送记录（不留孤儿行）。"""
        package = await _upload_package(
            client, platform, template_id=TEMPLATE_4G, version="3.2.0", with_file=True
        )
        pushed = await client.post(
            f"{PLATFORM}/ota/packages/{package['id']}/push",
            headers=platform,
            json={"clientProductId": PRODUCT_4G},
        )
        assert pushed.status_code == 200 and pushed.json()["requested"] >= 1

        deleted = await client.delete(f"{PLATFORM}/ota/packages/{package['id']}", headers=platform)
        assert deleted.status_code == 200, deleted.text
        await db.commit()
        left = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(OtaRecord)
                    .where(OtaRecord.ota_package_id == package["id"])
                )
            ).scalar_one()
        )
        assert left == 0, "删除固件包后仍留有推送记录"

    async def test_records_can_be_filtered_by_package(
        self, client: httpx.AsyncClient, platform: dict[str, str]
    ) -> None:
        """推送记录可按固件包筛选（运营排查「这批升级谁失败了」的入口）。"""
        package = await _upload_package(
            client, platform, template_id=TEMPLATE_4G, version="3.3.0", with_file=True
        )
        await client.post(
            f"{PLATFORM}/ota/packages/{package['id']}/push",
            headers=platform,
            json={"clientProductId": PRODUCT_4G},
        )
        response = await client.get(
            f"{PLATFORM}/ota/records", headers=platform, params={"packageId": package["id"]}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] >= 1
        assert all(row["packageId"] == package["id"] for row in body["records"])
