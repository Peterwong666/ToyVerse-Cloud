"""初始账号播种的幂等性（含「账号改名」这条真实事故路径）。

为什么单独有这个文件
--------------------
`_seed_admin_users` 原来**按 `account` 查重、按固定 `id` 插入**。这两个判据不一致，
于是当 `.env` 里的账号名被改动时：

* 按新名字查 → 查不到 → 认为「还没有」→ 拿同一个固定 id 再插一次
* → `sqlalchemy.exc.IntegrityError: UNIQUE constraint failed: user_accounts.id`

真实后果（实测）：容器入口脚本是「先播种、再起服务」，所以**应用启动即崩、无限重启**——
也就是「改一次 `.env` 里的管理员账号名，已有库的部署就挂掉」。

这里把四种路径都钉住：新建 / 重跑空操作 / **改名同步** / 名字被别的 id 占用。
其中 `test_renamed_account_is_synced_instead_of_crashing` 是**回归守卫**——
它在修复前的实现上会失败（抛 IntegrityError）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db import seed as seed_module
from app.models.identity import UserAccount

pytestmark = pytest.mark.asyncio

MERCHANT_ID = "u-merchant-admin"


async def _seed_tenants_and_admins(session: AsyncSession) -> None:
    """租户必须先落库：`user_accounts.tenant_id` 是指向 demo 租户的外键。"""
    await seed_module._seed_roles(session)
    await seed_module._seed_tenants(session)
    await seed_module._seed_admin_users(session)
    await session.commit()


async def _account_of(session: AsyncSession, user_id: str) -> UserAccount | None:
    return (
        await session.execute(select(UserAccount).where(UserAccount.id == user_id))
    ).scalar_one_or_none()


async def test_creates_all_admin_accounts(db: AsyncSession) -> None:
    """首次播种创建四个账号（平台运营已在 .env 中配置）。"""
    await _seed_tenants_and_admins(db)

    rows = (await db.execute(select(UserAccount))).scalars().all()
    assert {r.id for r in rows} == {
        "u-platform-admin",
        "u-platform-operator",
        "u-merchant-admin",
        "u-factory-admin",
    }
    assert {r.account for r in rows} == {
        settings.PLATFORM_ADMIN_ACCOUNT,
        settings.PLATFORM_OPERATOR_ACCOUNT,
        settings.MERCHANT_ADMIN_ACCOUNT,
        settings.FACTORY_ADMIN_ACCOUNT,
    }


async def test_reseed_is_noop_and_keeps_password(db: AsyncSession) -> None:
    """重跑不改任何东西——**尤其不改密码**（否则每次重启都会把线上密码重置回 .env）。"""
    await _seed_tenants_and_admins(db)
    before = {r.id: (r.account, r.password_hash) for r in (await db.execute(select(UserAccount))).scalars()}

    await _seed_tenants_and_admins(db)
    after = {r.id: (r.account, r.password_hash) for r in (await db.execute(select(UserAccount))).scalars()}

    assert before == after


async def test_renamed_account_is_synced_instead_of_crashing(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 回归守卫：`.env` 改了账号名 → 同步登录名，**不能抛 IntegrityError**。

    修复前的实现会在这里抛
    `sqlalchemy.exc.IntegrityError: UNIQUE constraint failed: user_accounts.id`，
    而在容器里表现为应用启动即崩。
    """
    await _seed_tenants_and_admins(db)
    original = await _account_of(db, MERCHANT_ID)
    assert original is not None
    original_hash = original.password_hash

    monkeypatch.setattr(settings, "MERCHANT_ADMIN_ACCOUNT", "13800000099")
    await _seed_tenants_and_admins(db)

    renamed = await _account_of(db, MERCHANT_ID)
    assert renamed is not None
    assert renamed.account == "13800000099", "账号名应同步为 .env 中的新值"
    assert renamed.password_hash == original_hash, "改名不应重置密码"

    # 不能因为改名而多出一行
    rows = (await db.execute(select(UserAccount))).scalars().all()
    assert len(rows) == 4


async def test_renamed_account_keeps_synced_on_third_run(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """改名后再次重跑应当是**空操作**（幂等），而不是每次都写一遍。"""
    await _seed_tenants_and_admins(db)
    monkeypatch.setattr(settings, "MERCHANT_ADMIN_ACCOUNT", "13800000099")
    await _seed_tenants_and_admins(db)
    first = await _account_of(db, MERCHANT_ID)
    assert first is not None
    hash_after_rename = first.password_hash

    await _seed_tenants_and_admins(db)
    again = await _account_of(db, MERCHANT_ID)
    assert again is not None
    assert again.account == "13800000099"
    assert again.password_hash == hash_after_rename


async def test_account_name_taken_by_other_id_is_skipped(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """目标登录名被**别的 id** 占用时跳过创建（否则会撞 `uq_user_accounts_account`）。

    这类情况在真实环境里可能出现：运维手工建过一个同名账号。
    """
    await _seed_tenants_and_admins(db)

    # 抢先占住「新名字」，但用一个不同的 id
    db.add(
        UserAccount(
            id="u-manual",
            account="13800000099",
            password_hash="$2b$12$placeholderplaceholderplaceholderplaceholderplaceholder",
            nickname="手工账号",
            role_code="MERCHANT_ADMIN",
            tenant_id="t-001",
            status="ACTIVE",
            must_change_password=False,
        )
    )
    await db.commit()

    monkeypatch.setattr(settings, "MERCHANT_ADMIN_ACCOUNT", "13800000099")
    await _seed_tenants_and_admins(db)   # 不应抛异常

    # 原账号保持原样，手工账号未被改动
    merchant = await _account_of(db, MERCHANT_ID)
    manual = await _account_of(db, "u-manual")
    assert merchant is not None and manual is not None
    assert merchant.account != "13800000099", "名字被占用时应保持原登录名"
    assert manual.nickname == "手工账号"
