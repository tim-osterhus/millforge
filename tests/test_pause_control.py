"""Independent gate ordering, path eligibility and durable invalidation probes."""

import asyncio
import os

import pytest

from millforge.pause_control import (
    NativePauseControl,
    PROFILE,
    PROFILE_DIGEST,
    local_file_supported,
)

P = {**PROFILE, "profile_digest": PROFILE_DIGEST}


def gate(root):
    value = NativePauseControl(root, eligible=True)
    value.ready("session", lambda: None)
    value.set_journal(lambda _: None)
    return value


@pytest.mark.asyncio
async def test_admission_before_unsupported_entry(tmp_path):
    value = gate(tmp_path)
    with value.reservation(P, "runs.pause", None):
        value.accept("pause", "digest", "runs.pause", "pause-id")
    entered = []

    async def effect():
        async with value.effect("shell", supported=False):
            entered.append(True)

    task = asyncio.create_task(effect())
    await asyncio.sleep(0.03)
    assert not entered and value.snapshot()["state"] == "held"
    with value.reservation(P, "runs.resume", "pause-id"):
        value.accept("resume", "digest2", "runs.resume", "pause-id")
    await asyncio.sleep(0.02)
    assert not entered  # durable settlement barrier
    value.confirm_settlement("resume")
    await task
    assert entered and not value.snapshot()["eligible"]
    with pytest.raises(RuntimeError, match="unqualified"):
        with value.reservation(P, "runs.pause", None):
            pytest.fail("unsupported effect admitted")


@pytest.mark.asyncio
async def test_unsupported_entry_before_admission(tmp_path):
    value = gate(tmp_path)
    async with value.effect("shell", supported=False):
        with pytest.raises(RuntimeError, match="unqualified"):
            with value.reservation(P, "runs.pause", None):
                pytest.fail("stale eligibility")


@pytest.mark.asyncio
async def test_reservation_exception_releases_gate(tmp_path):
    value = gate(tmp_path)
    with pytest.raises(ValueError):
        with value.reservation(P, "runs.pause", None):
            raise ValueError("storage failure")
    import threading

    acquired = []

    def reserve_again():
        with value.reservation(P, "runs.pause", None):
            acquired.append(True)

    thread = threading.Thread(target=reserve_again)
    thread.start()
    thread.join(1)
    assert acquired == [True] and not thread.is_alive()
    async with value.effect("write", supported=True):
        assert value.snapshot()["active_effects"] == 1


@pytest.mark.asyncio
async def test_held_invalidation_must_be_durable_before_effect(tmp_path):
    value = gate(tmp_path)
    with value.reservation(P, "runs.pause", None):
        value.accept("pause", "digest", "runs.pause", "pause-id")

    parked = asyncio.create_task(value.checkpoint())
    await asyncio.sleep(0.02)

    def broken(_):
        raise RuntimeError("storage unavailable")

    value.set_journal(broken)
    with pytest.raises(RuntimeError, match="storage unavailable"):
        value.invalidate("finalizing")
    assert value.snapshot()["state"] == "held"
    assert value.snapshot()["invalidation_pending"]
    with pytest.raises(RuntimeError, match="native_profile_unqualified"):
        with value.reservation(P, "runs.resume", "pause-id"):
            pytest.fail("failed invalidation admitted a release")
    events = []
    value.set_journal(lambda evidence: events.append(evidence))
    value.invalidate("finalizing")
    assert len(events) == 1 and value.snapshot()["state"] == "closed"
    await parked


@pytest.mark.parametrize(
    "kind", ["regular", "new", "escape", "symlink", "fifo", "directory"]
)
def test_actual_path_scope(tmp_path, kind):
    path = tmp_path / "target"
    if kind == "regular":
        path.write_text("real")
    elif kind == "escape":
        path = tmp_path.parent / "outside"
    elif kind == "symlink":
        (tmp_path / "real").write_text("real")
        path.symlink_to(tmp_path / "real")
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    assert local_file_supported(tmp_path, str(path), writing=True) == (
        kind in {"regular", "new"}
    )


@pytest.mark.asyncio
async def test_zero_effects_does_not_apply_before_owner_checkpoint(tmp_path):
    value = gate(tmp_path)
    with value.reservation(P, "runs.pause", None):
        value.accept("pause", "digest", "runs.pause", "pause-id")
    assert value.snapshot()["active_effects"] == 0
    assert value.snapshot()["state"] == "pause_pending"
    await asyncio.sleep(0.02)
    assert value.snapshot()["state"] == "pause_pending"
    parked = asyncio.create_task(value.checkpoint())
    await asyncio.sleep(0.02)
    assert value.snapshot()["parked"] and value.snapshot()["state"] == "held"
    with value.reservation(P, "runs.resume", "pause-id"):
        value.accept("resume", "digest2", "runs.resume", "pause-id")
    await asyncio.sleep(0.02)
    assert not parked.done()
    value.confirm_settlement("resume")
    await parked
    assert not value.snapshot()["parked"]
