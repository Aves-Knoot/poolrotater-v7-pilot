"""Player help and pool labels stay accurate in Taiko without changing other modes."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

from types import SimpleNamespace
from unittest.mock import Mock

from bot.bot import LobbyBot


def make_bot(ruleset_id=1, *, moderator=False, admin=False):
    bot = LobbyBot.__new__(LobbyBot)
    bot.cfg = {"ruleset_id": ruleset_id, "user_commands_enabled": True}
    bot.prefix = "."
    bot.say = Mock()
    bot._is_admin = lambda _uid: admin
    bot._is_moderator = lambda _uid: moderator or admin
    bot._is_project_banned = lambda _uid: False
    bot._allow_public_command_burst = lambda _uid, _cmd: True
    bot._discord_command_reply_channel_id = None
    bot._mania_key_label = lambda: "4K"
    bot._difficulty_vote_step = lambda: 0.5
    bot.pool = SimpleNamespace(
        loved_mode=False,
        source_mode="taiko-variety" if ruleset_id == 1 else "fallback",
        source_mode_label=Mock(return_value="Taiko NM variety native only"),
        maps=[],
        sr_min=3.0,
        sr_max=5.0,
        max_length_label=lambda: "4m",
        max_map_age_label=lambda: "off",
        min_passcount=23,
        set_max_map_age_years=Mock(return_value=2),
    )
    bot.gameplay_player_ids = lambda: []
    bot.auto_difficulty_pp_values = lambda _ids: ([], False, 0)
    bot._community_bias_overlay = lambda: 0.0
    bot.manual_range = False
    return bot


def sent_text(bot):
    return "\n".join(call.args[0] for call in bot.say.call_args_list)


def test_taiko_help_and_commands_topics_use_taiko_descriptions():
    bot = make_bot()
    for command in (".help map", ".commands map"):
        bot.say.reset_mock()
        bot.handle_command(7, command)
        assert "current map" in sent_text(bot).lower()
        assert "mania lobbies" not in sent_text(bot).lower()

    bot.say.reset_mock()
    bot.handle_command(7, ".help pp")
    pp = sent_text(bot).lower()
    assert "taiko nm reference" in pp
    assert "mods" not in pp and "accuracy" not in pp

    bot.say.reset_mock()
    bot.handle_command(7, ".help variety")
    variety = sent_text(bot).lower()
    assert "taiko nm variety" in variety
    assert "farm%" not in variety and "stream%" not in variety


def test_taiko_help2_lists_only_supported_player_map_controls():
    bot = make_bot()
    bot.handle_command(7, ".help2")
    help_text = sent_text(bot)
    assert ".map" in help_text and ".variety" in help_text
    assert ".pp " in help_text
    for unsupported in (".farm", ".stream", ".jump", ".aim", ".dt", ".nm", ".pp [mods]"):
        assert unsupported not in help_text
    assert "Mod:" not in help_text and "Admin:" not in help_text


def test_taiko_help2_preserves_moderator_and_admin_sections():
    moderator_bot = make_bot(moderator=True)
    moderator_bot.handle_command(7, ".help2")
    assert "Mod:" in sent_text(moderator_bot)
    assert "Admin:" not in sent_text(moderator_bot)

    admin_bot = make_bot(admin=True)
    admin_bot.handle_command(7, ".help2")
    assert "Mod:" in sent_text(admin_bot)
    assert "Admin:" in sent_text(admin_bot)
    assert ".maxage" not in sent_text(admin_bot)
    assert "minpasses" not in sent_text(admin_bot)

    standard_admin = make_bot(0, admin=True)
    standard_admin.handle_command(7, ".help2")
    assert ".maxage" in sent_text(standard_admin)
    assert "minpasses" in sent_text(standard_admin)


def test_pool_uses_taiko_source_label_and_retains_other_mode_fallback():
    taiko = make_bot(1)
    taiko.handle_command(7, ".pool")
    assert "Taiko NM variety native only" in sent_text(taiko)
    assert "(fallback)" not in sent_text(taiko)

    standard = make_bot(0)
    standard.handle_command(7, ".pool")
    assert "(fallback)" in sent_text(standard)


def test_taiko_maxage_and_minpass_aliases_refuse_without_mutation():
    moderator = make_bot(1, moderator=True)
    moderator.cfg["max_map_age_years"] = 7
    before_cfg = dict(moderator.cfg)
    moderator.handle_command(7, ".maxage 1")
    assert "not available in Taiko" in sent_text(moderator)
    assert moderator.cfg == before_cfg
    moderator.pool.set_max_map_age_years.assert_not_called()

    for setting in ("passes", "minpasses", "maxage", "farmshare", "streamshare"):
        admin = make_bot(1, admin=True)
        admin.cfg["min_passcount"] = 23
        before_cfg = dict(admin.cfg)
        admin._reload_pool_filter = Mock()
        admin._persist_config_updates = Mock()
        admin.handle_command(7, f".set {setting} 1000")
        assert "not available in Taiko" in sent_text(admin)
        assert admin.cfg == before_cfg
        assert admin.pool.min_passcount == 23
        admin._reload_pool_filter.assert_not_called()
        admin._persist_config_updates.assert_not_called()


def test_taiko_settings_and_set_usage_do_not_present_standard_only_filters():
    admin = make_bot(1, admin=True)
    admin.handle_command(7, ".set minpasses")
    usage = sent_text(admin)
    assert "Usage:" in usage
    assert "minpasses" not in usage
    assert "maxage" not in usage


def test_taiko_moderator_topic_help_refuses_unsupported_age_filter():
    bot = make_bot(moderator=True)
    bot.handle_command(7, ".help maxage")
    assert "not available in Taiko" in sent_text(bot)
    assert "<years" not in sent_text(bot)

