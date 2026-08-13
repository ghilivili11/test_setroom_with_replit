"""Minimal Highrise bot with welcome messages and host-controlled room switching."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from uuid import UUID

from highrise import BaseBot
from highrise.__main__ import BotDefinition, main as sdk_main
from highrise.models import AnchorPosition, Position, SessionMetadata, User


CONFIG_PATH = Path(__file__).with_name("bot_config.json")


def valid_room_id(room_id: str) -> bool:
    """Highrise room IDs are UUIDs."""
    try:
        UUID(room_id)
    except ValueError:
        return False
    return True


def load_room_id() -> str:
    """Read the active room from local persistence, or bootstrap from the environment."""
    if CONFIG_PATH.exists():
        try:
            config = json.loads(CONFIG_PATH.read_text())
            room_id = config.get("room_id")
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Could not read {CONFIG_PATH}: {exc}") from exc

        if isinstance(room_id, str) and valid_room_id(room_id):
            return room_id

        raise RuntimeError(f"{CONFIG_PATH} must contain a valid UUID room_id.")

    room_id = os.environ.get("HIGHRISE_ROOM_ID", "").strip()

    if not valid_room_id(room_id):
        raise RuntimeError(
            "Set HIGHRISE_ROOM_ID to a valid Highrise room UUID before the first run."
        )

    save_room_id(room_id)
    return room_id


def save_room_id(room_id: str) -> None:
    """Persist only the room ID so it survives the reconnect."""
    CONFIG_PATH.write_text(json.dumps({"room_id": room_id}, indent=4) + "\n")


class MinimalHighriseBot(BaseBot):
    """The only bot behaviors needed for this test project."""

    def __init__(
        self,
        owner_id: str,
        room_id: str,
        switch_requested: asyncio.Event,
        announce_new_room: bool,
    ) -> None:
        self.owner_id = owner_id
        self.room_id = room_id
        self.switch_requested = switch_requested
        self.announce_new_room = announce_new_room
        self.requested_room: str | None = None

    async def on_start(self, session_metadata: SessionMetadata) -> None:
        # The SDK calls on_start after the WebSocket handshake and room session
        # metadata have been received, so this is the connected-room point.
        print(f"Successfully connected to room: {self.room_id}")

        if self.announce_new_room:
            try:
                await self.highrise.chat(
                    "✅ Successfully connected to the new room."
                )
            except Exception as exc:
                print(f"ERROR: Could not send connection confirmation: {exc}")
            else:
                self.announce_new_room = False

    async def on_user_join(
        self, user: User, position: Position | AnchorPosition
    ) -> None:
        # on_user_join is the SDK event for a user entering the current room.
        await self.highrise.chat(f"Welcome, @{user.username}!")

    async def on_chat(self, user: User, message: str) -> None:
        # on_chat is the SDK event for room-wide chat messages.
        parts = message.strip().split()

        if not parts or parts[0].lower() != "!setroom":
            return

        print("Received !setroom command")

        if user.id != self.owner_id:
            await self.highrise.chat("Only the authorized host can use !setroom.")
            return

        if len(parts) != 2:
            await self.highrise.chat("Usage: !setroom ROOM_ID")
            return

        requested_room = parts[1].strip()
        print(f"Requested new room: {requested_room}")

        if not valid_room_id(requested_room):
            await self.highrise.chat(
                "Invalid room ID. Use a valid Highrise room UUID."
            )
            return

        if requested_room == self.room_id:
            await self.highrise.chat("I am already connected to that room.")
            return

        try:
            print("Saving new room ID...")
            save_room_id(requested_room)
        except OSError as exc:
            print(f"ERROR: Could not save new room ID: {exc}")
            await self.highrise.chat("Could not save the new room ID.")
            return

        self.requested_room = requested_room
        self.switch_requested.set()


async def run_controller() -> None:
    """Run the SDK and restart its fixed-room connection when !setroom is used."""
    token = os.environ.get("HIGHRISE_BOT_TOKEN", "").strip()
    owner_id = os.environ.get("HIGHRISE_OWNER_ID", "").strip()

    if not token:
        raise RuntimeError("HIGHRISE_BOT_TOKEN is not set.")

    if not owner_id:
        raise RuntimeError("HIGHRISE_OWNER_ID is not set.")

    room_id = load_room_id()
    announce_new_room = False

    while True:
        print(f"Starting bot with room: {room_id}")
        print(f"Current room: {room_id}")

        switch_requested = asyncio.Event()

        bot = MinimalHighriseBot(
            owner_id=owner_id,
            room_id=room_id,
            switch_requested=switch_requested,
            announce_new_room=announce_new_room,
        )

        # The SDK accepts a BotDefinition containing the bot, room ID, and
        # token. Its runner reconnects to this same room, so changing rooms
        # requires stopping this task and starting a new SDK session.
        sdk_task = asyncio.create_task(
            sdk_main([BotDefinition(bot, room_id, token)])
        )

        switch_task = asyncio.create_task(switch_requested.wait())

        done, _ = await asyncio.wait(
            {sdk_task, switch_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if switch_task in done:
            new_room_id = bot.requested_room

            if new_room_id is None:
                raise RuntimeError(
                    "Room switch was requested without a new room ID."
                )

            print("Restarting/reconnecting...")

            sdk_task.cancel()

            try:
                await sdk_task
            except asyncio.CancelledError:
                pass

            room_id = new_room_id
            announce_new_room = True
            continue

        switch_task.cancel()
        await sdk_task

        raise RuntimeError(
            "The Highrise SDK stopped without a room switch."
        )


if __name__ == "__main__":
    print("Starting bot...")

    try:
        asyncio.run(run_controller())
    except KeyboardInterrupt:
        print("Bot stopped.")
    except Exception as exc:
        print(f"ERROR: Bot stopped: {exc}")
        raise SystemExit(1) from exc
