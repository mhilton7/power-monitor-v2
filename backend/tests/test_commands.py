from __future__ import annotations

import pytest
from backend.app.main import session_factory
from backend.app.models import Device, Home, User
from backend.app.schemas.device import CommandResult
from backend.app.services.commands import apply_command_results, create_command, deliver_commands


@pytest.mark.asyncio
async def test_command_delivery_is_idempotent_and_progress_monotonic() -> None:
    async with session_factory() as session:
        home = Home(name="Command Home")
        user = User(email="command@example.com", display_name="Commander", password_hash="not-used")
        session.add_all((home, user))
        await session.flush()
        device = Device(
            home_id=home.id,
            friendly_name="Sensor",
            pzem_variant="pzem004t-v4-classic-candidate",
            ct_rating_a=100,
        )
        session.add(device)
        await session.flush()
        command, _token = await create_command(
            session,
            device_id=device.id,
            command_type="reboot",
            issued_by_user_id=user.id,
            idempotency_key="reboot-unique-001",
        )
        same, _ = await create_command(
            session,
            device_id=device.id,
            command_type="reboot",
            issued_by_user_id=user.id,
            idempotency_key="reboot-unique-001",
        )
        assert same.id == command.id
        envelopes = await deliver_commands(session, device.id)
        assert [item.command_id for item in envelopes] == [command.id]
        await apply_command_results(
            session,
            device.id,
            [
                CommandResult(
                    command_id=command.id,
                    state="running",
                    progress_percent=50,
                    result_code="IN_PROGRESS",
                )
            ],
        )
        with pytest.raises(Exception, match="progress cannot move backward"):
            await apply_command_results(
                session,
                device.id,
                [
                    CommandResult(
                        command_id=command.id,
                        state="running",
                        progress_percent=40,
                        result_code="IN_PROGRESS",
                    )
                ],
            )
