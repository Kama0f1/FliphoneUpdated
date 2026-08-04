from __future__ import annotations

import unittest
from types import SimpleNamespace

import config
from access_control import is_trusted_moderator, owner_only, trusted_moderator_only


class _Bot:
    async def is_owner(self, user) -> bool:
        return user.id == 1


class AccessControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_and_configured_moderator_are_trusted(self):
        bot = _Bot()
        configured_id = next(iter(config.TRUSTED_MOD_IDS))

        self.assertTrue(await is_trusted_moderator(bot, SimpleNamespace(id=1)))
        self.assertTrue(await is_trusted_moderator(bot, SimpleNamespace(id=configured_id)))
        self.assertFalse(await is_trusted_moderator(bot, SimpleNamespace(id=999)))

    async def test_decorators_cover_command_and_interaction_layers(self):
        async def callback(_ctx):
            return None

        for decorator in (trusted_moderator_only(), owner_only()):
            wrapped = decorator(callback)
            self.assertTrue(getattr(wrapped, "__commands_checks__", []))
            self.assertTrue(getattr(wrapped, "__discord_app_commands_checks__", []))


if __name__ == "__main__":
    unittest.main()
